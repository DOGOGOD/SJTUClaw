"""Session-scoped microsandbox runtime used by native SJTUClaw tools.

The public ``SandboxManager`` surface is synchronous because Tool handlers are
synchronous.  The microsandbox Python SDK is async, so the concrete backend
keeps one dedicated event loop thread and owns every SDK object on that loop.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import os
import posixpath
import re
import shlex
import struct
import subprocess
import threading
import uuid
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Coroutine, Protocol

from claw.config import DATA_DIR
from claw.paths import is_frozen
from claw.sandbox.config import SandboxConfig
from claw.utils import atomic_write

logger = logging.getLogger(__name__)

GUEST_WORKSPACE = "/workspace"
GUEST_PROJECT_VENV = f"{GUEST_WORKSPACE}/.venv"
GUEST_RUNTIME_VENV = "/opt/sjtuclaw/project-venv"
_GUEST_PROJECT_ENV_SYNC = "/opt/sjtuclaw/project_env_sync.py"
_MAX_OUTPUT_BYTES = 64 * 1024
_STREAM_CAPTURE_BYTES = _MAX_OUTPUT_BYTES + 4 * 1024
_STREAM_TAIL_BYTES = 4 * 1024
_MAX_STREAM_BYTES = 8 * 1024 * 1024
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_INVALID_EXPORT_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_IMAGE_SUBSYSTEM_WINDOWS_GUI = 2
_IMAGE_SUBSYSTEM_WINDOWS_CUI = 3
_PE_SUBSYSTEM_RELATIVE_OFFSET = 68
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class SandboxError(RuntimeError):
    """A user-actionable sandbox error."""


def _pe_subsystem_offset(image: bytes | bytearray) -> int:
    """Return the PE optional-header subsystem field offset."""
    if len(image) < 64 or image[:2] != b"MZ":
        raise SandboxError("microsandbox 的 msb.exe 不是有效的 Windows PE 文件")
    pe_offset = struct.unpack_from("<I", image, 0x3C)[0]
    if pe_offset + 24 > len(image) or image[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise SandboxError("microsandbox 的 msb.exe 缺少有效的 PE 文件头")
    optional_size = struct.unpack_from("<H", image, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    subsystem_offset = optional_offset + _PE_SUBSYSTEM_RELATIVE_OFFSET
    if optional_size < _PE_SUBSYSTEM_RELATIVE_OFFSET + 2:
        raise SandboxError("microsandbox 的 msb.exe PE 可选头不完整")
    if subsystem_offset + 2 > len(image):
        raise SandboxError("microsandbox 的 msb.exe PE 子系统字段越界")
    magic = struct.unpack_from("<H", image, optional_offset)[0]
    if magic not in {0x10B, 0x20B}:
        raise SandboxError("microsandbox 的 msb.exe PE 格式不受支持")
    return subsystem_offset


def _prepare_windows_gui_msb(
    source: Path,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Cache a GUI-subsystem copy of msb.exe for the frozen desktop app.

    microsandbox 0.6.x bundles ``msb.exe`` as a console application. Windows
    therefore opens a terminal every time the native SDK launches it from our
    windowed executable. The SDK already communicates over redirected handles,
    so a GUI-subsystem copy preserves its protocol while preventing automatic
    console allocation.
    """
    try:
        original = source.read_bytes()
    except OSError as exc:
        raise SandboxError(f"无法读取 microsandbox 运行文件: {source}") from exc

    subsystem_offset = _pe_subsystem_offset(original)
    subsystem = struct.unpack_from("<H", original, subsystem_offset)[0]
    if subsystem == _IMAGE_SUBSYSTEM_WINDOWS_GUI:
        return source
    if subsystem != _IMAGE_SUBSYSTEM_WINDOWS_CUI:
        raise SandboxError(
            f"microsandbox 的 msb.exe 使用未知的 PE 子系统类型: {subsystem}"
        )

    digest = hashlib.sha256(original).hexdigest()[:16]
    target_dir = cache_dir or (DATA_DIR / "runtime" / "microsandbox")
    runtime_root = target_dir / digest
    target = runtime_root / "bin" / "msb.exe"
    bundled_libkrunfw = source.parent.parent / "lib" / "libkrunfw.dll"
    target_libkrunfw = runtime_root / "lib" / "libkrunfw.dll"
    try:
        libkrunfw = bundled_libkrunfw.read_bytes()
    except OSError as exc:
        raise SandboxError(
            f"无法读取 microsandbox 虚拟机运行库: {bundled_libkrunfw}"
        ) from exc

    target_valid = False
    if target.is_file() and target_libkrunfw.is_file():
        try:
            cached = target.read_bytes()
            cached_offset = _pe_subsystem_offset(cached)
            target_valid = (
                struct.unpack_from("<H", cached, cached_offset)[0]
                == _IMAGE_SUBSYSTEM_WINDOWS_GUI
                and target_libkrunfw.stat().st_size == len(libkrunfw)
            )
        except (OSError, SandboxError):
            pass
    if target_valid:
        return target

    patched = bytearray(original)
    struct.pack_into(
        "<H",
        patched,
        subsystem_offset,
        _IMAGE_SUBSYSTEM_WINDOWS_GUI,
    )
    try:
        atomic_write(target, bytes(patched))
        atomic_write(target_libkrunfw, libkrunfw)
    except OSError as exc:
        raise SandboxError(
            f"无法创建无终端窗口的 microsandbox 运行文件: {target}"
        ) from exc
    return target


def _configure_frozen_windows_microsandbox(_msb: Any) -> None:
    """Point the native SDK at a no-console runtime in frozen Windows builds."""
    if os.name != "nt" or not is_frozen() or os.getenv("MSB_PATH", "").strip():
        return

    from microsandbox._runtime import msb_path

    bundled_msb = msb_path()
    gui_msb = _prepare_windows_gui_msb(bundled_msb)
    cached_libkrunfw = gui_msb.parent.parent / "lib" / "libkrunfw.dll"
    # microsandbox's Python package initializes the native runtime path during
    # import. Its setter is backed by a write-once cell, so trying to replace
    # that path here is silently ignored. The native resolver checks these
    # environment overrides on every resolution and gives them highest
    # precedence, including after the package has already been imported.
    os.environ["MSB_PATH"] = str(gui_msb)
    if not os.getenv("MSB_LIBKRUNFW_PATH", "").strip():
        os.environ["MSB_LIBKRUNFW_PATH"] = str(cached_libkrunfw)


@dataclass(frozen=True, slots=True)
class SandboxCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    cwd: str
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_limited: bool = False

    @property
    def ok(self) -> bool:
        return (
            not self.timed_out
            and not self.output_limited
            and self.exit_code == 0
        )


@dataclass(frozen=True, slots=True)
class SandboxEntry:
    name: str
    kind: str
    size: int


@dataclass(frozen=True, slots=True)
class _BoundedExecOutput:
    exit_code: int
    stdout_bytes: bytes
    stderr_bytes: bytes
    stdout_tail_bytes: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    output_limited: bool = False


@dataclass(slots=True)
class _BoundedCapture:
    """Retain a small head/tail while counting the complete byte stream."""

    head_limit: int
    tail_limit: int
    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    seen: int = 0

    def append(self, payload: bytes) -> None:
        data = bytes(payload)
        self.seen += len(data)
        remaining = self.head_limit - len(self.head)
        if remaining > 0:
            self.head.extend(data[:remaining])
        if self.tail_limit <= 0:
            return
        if len(data) >= self.tail_limit:
            self.tail[:] = data[-self.tail_limit:]
            return
        self.tail.extend(data)
        excess = len(self.tail) - self.tail_limit
        if excess > 0:
            del self.tail[:excess]

    @property
    def truncated(self) -> bool:
        return self.seen > len(self.head)


class SandboxBackend(Protocol):
    """Small synchronous boundary around the async SDK (also easy to fake)."""

    def create(
        self,
        *,
        name: str,
        volume_name: str,
        host_workspace: Path | None,
        config: SandboxConfig,
    ) -> Any: ...

    def stop(self, sandbox: Any, name: str) -> None: ...

    def remove_volume(self, volume_name: str) -> None: ...

    def alive(self, sandbox: Any) -> bool: ...

    def shell(
        self,
        sandbox: Any,
        command: str,
        *,
        cwd: str,
        timeout: int,
    ) -> Any: ...

    def exists(self, sandbox: Any, path: str) -> bool: ...

    def stat(self, sandbox: Any, path: str) -> Any: ...

    def list(self, sandbox: Any, path: str) -> list[Any]: ...

    def read_limited(
        self, sandbox: Any, path: str, max_bytes: int
    ) -> tuple[bytes, bool]: ...

    def write(self, sandbox: Any, path: str, data: bytes) -> None: ...

    def rename(self, sandbox: Any, source: str, destination: str) -> None: ...

    def remove(self, sandbox: Any, path: str) -> None: ...

    def mkdir_parents(self, sandbox: Any, path: str) -> None: ...

    def copy_from_host(self, sandbox: Any, host: Path, guest: str) -> None: ...

    def copy_to_host(self, sandbox: Any, guest: str, host: Path) -> None: ...

    def close(self) -> None: ...


class _AsyncBridge:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="sjtuclaw-microsandbox",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()
        self._loop.close()

    def call(self, coro: Coroutine[Any, Any, Any], *, timeout: float | None = None) -> Any:
        if self._closed:
            coro.close()
            raise SandboxError("sandbox 运行层已经关闭")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)


class MicrosandboxBackend:
    """Concrete adapter for microsandbox 0.6.x.

    Import is intentionally lazy so installing SJTUClaw without the optional
    sandbox extra still works in ``off`` and unavailable ``auto`` modes.
    """

    def __init__(self) -> None:
        try:
            import microsandbox as msb
        except Exception as exc:  # native loader failures are common/actionable
            raise SandboxError(
                "microsandbox 不可用。请安装 SJTUClaw 的 sandbox 可选依赖，"
                "并确认 Windows Hypervisor Platform 已启用。"
            ) from exc
        try:
            _configure_frozen_windows_microsandbox(msb)
        except Exception as exc:
            if isinstance(exc, SandboxError):
                raise
            raise SandboxError(
                f"配置 microsandbox Windows 运行文件失败: {exc}"
            ) from exc
        self._msb = msb
        self._bridge = _AsyncBridge()

    def _call(
        self,
        function: Any,
        *args: Any,
        _wait_timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Construct and await SDK coroutines on the SDK's event-loop thread."""
        async def _invoke() -> Any:
            return await function(*args, **kwargs)

        return self._bridge.call(_invoke(), timeout=_wait_timeout)

    def create(
        self,
        *,
        name: str,
        volume_name: str,
        host_workspace: Path | None,
        config: SandboxConfig,
    ) -> Any:
        msb = self._msb
        stat_mode = config.stat_virtualization
        host_permissions = msb.HostPermissions.PRIVATE
        if stat_mode == "auto":
            # microsandbox's metadata sidecar is not currently compatible
            # with files created inside Windows host-directory mounts.  Keep
            # the stronger virtualization on POSIX hosts. On Windows, use the
            # relaxed overlay and mirror ordinary rwx bits so guest-created
            # files and project console scripts retain usable permissions.
            if os.name == "nt":
                stat_mode = "relaxed"
                host_permissions = msb.HostPermissions.MIRROR
            else:
                stat_mode = "strict"
        stat_virtualization = {
            "strict": msb.StatVirtualization.STRICT,
            "relaxed": msb.StatVirtualization.RELAXED,
            "off": msb.StatVirtualization.OFF,
        }[stat_mode]
        if host_workspace is None:
            mount = msb.MountConfig(
                kind=msb.MountKind.NAMED,
                named=volume_name,
                named_mode="ensure-exists",
                named_kind="dir",
                quota_mib=config.workspace_quota_mib,
                nosuid=True,
                nodev=True,
                stat_virtualization=stat_virtualization,
                host_permissions=host_permissions,
            )
        else:
            mount = msb.MountConfig(
                kind=msb.MountKind.BIND,
                bind=str(host_workspace),
                quota_mib=config.workspace_quota_mib,
                nosuid=True,
                nodev=True,
                stat_virtualization=stat_virtualization,
                host_permissions=host_permissions,
            )

        network = (
            msb.Network.none()
            if config.network == "none"
            else msb.Network.from_profiles("public")
        )
        try:
            return self._call(
                msb.Sandbox.create,
                name,
                image=config.image,
                cpus=config.cpus,
                memory=config.memory_mib,
                workdir=GUEST_WORKSPACE,
                shell="/bin/sh",
                volumes={GUEST_WORKSPACE: mount},
                network=network,
                security=config.security,
                max_duration=float(config.max_duration_s),
                idle_timeout=float(config.idle_timeout_s),
                ephemeral=True,
                labels={"owner": "sjtuclaw"},
                pull_policy="if-missing",
                replace=True,
                _wait_timeout=180,
            )
        except Exception as exc:
            raise SandboxError(f"启动 microsandbox 失败: {exc}") from exc

    def stop(self, sandbox: Any, name: str) -> None:
        async def _stop() -> None:
            termination_errors: list[Exception] = []
            try:
                await sandbox.stop(timeout=10.0)
            except self._msb.SandboxNotFoundError:
                return
            except Exception as stop_exc:
                termination_errors.append(stop_exc)
                try:
                    await sandbox.kill(timeout=5.0)
                except self._msb.SandboxNotFoundError:
                    return
                except Exception as kill_exc:
                    termination_errors.append(kill_exc)
            try:
                await self._msb.Sandbox.remove(name)
            except self._msb.SandboxNotFoundError:
                return
            except Exception as remove_exc:
                details = "; ".join(str(error) for error in termination_errors)
                if details:
                    raise SandboxError(
                        f"停止 sandbox {name} 失败: {details}; "
                        f"删除运行记录失败: {remove_exc}"
                    ) from remove_exc
                raise SandboxError(
                    f"删除 sandbox {name} 的运行记录失败: {remove_exc}"
                ) from remove_exc

        self._bridge.call(_stop(), timeout=20)

    def remove_volume(self, volume_name: str) -> None:
        async def _remove() -> None:
            try:
                await self._msb.Volume.remove(volume_name)
            except self._msb.VolumeNotFoundError:
                return

        self._bridge.call(_remove(), timeout=20)

    def alive(self, sandbox: Any) -> bool:
        try:
            self._call(sandbox.ping, _wait_timeout=5)
            return True
        except Exception:
            return False

    def shell(
        self,
        sandbox: Any,
        command: str,
        *,
        cwd: str,
        timeout: int,
    ) -> Any:
        async def _shell_stream() -> _BoundedExecOutput:
            handle = await sandbox.shell_stream(
                command,
                cwd=cwd,
                timeout=float(timeout),
            )
            stdout = _BoundedCapture(
                head_limit=_STREAM_CAPTURE_BYTES,
                tail_limit=_STREAM_TAIL_BYTES,
            )
            stderr = _BoundedCapture(
                head_limit=_STREAM_CAPTURE_BYTES,
                tail_limit=0,
            )
            exit_code: int | None = None
            output_limited = False
            deadline = asyncio.get_running_loop().time() + float(timeout)

            def consume(event: Any) -> None:
                nonlocal exit_code
                event_type = str(getattr(event, "event_type", ""))
                data = bytes(getattr(event, "data", None) or b"")
                if event_type == "stdout":
                    stdout.append(data)
                elif event_type in {"stderr", "failed", "stdin_error"}:
                    stderr.append(data)
                    if event_type == "failed":
                        exit_code = int(getattr(event, "code", None) or 1)
                elif event_type == "exited":
                    exit_code = int(getattr(event, "code", None) or 0)

            async def next_event(until: float) -> Any:
                remaining = until - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                return await asyncio.wait_for(handle.recv(), timeout=remaining)

            try:
                while True:
                    event = await next_event(deadline)
                    if event is None:
                        break
                    consume(event)
                    if (
                        stdout.seen + stderr.seen > _MAX_STREAM_BYTES
                        and not output_limited
                    ):
                        output_limited = True
                        with suppress(Exception):
                            await handle.kill()
                        deadline = min(
                            deadline,
                            asyncio.get_running_loop().time() + 5.0,
                        )
            except (
                TimeoutError,
                asyncio.TimeoutError,
                self._msb.ExecTimeoutError,
            ):
                with suppress(Exception):
                    await handle.kill()
                drain_deadline = asyncio.get_running_loop().time() + 5.0
                with suppress(Exception):
                    while True:
                        event = await next_event(drain_deadline)
                        if event is None:
                            break
                        consume(event)
                if not output_limited:
                    raise TimeoutError
            except asyncio.CancelledError:
                with suppress(Exception):
                    await handle.kill()
                raise

            return _BoundedExecOutput(
                exit_code=exit_code if exit_code is not None else -1,
                stdout_bytes=bytes(stdout.head),
                stderr_bytes=bytes(stderr.head),
                stdout_tail_bytes=bytes(stdout.tail),
                stdout_truncated=stdout.truncated,
                stderr_truncated=stderr.truncated,
                output_limited=output_limited,
            )

        return self._bridge.call(
            _shell_stream(),
            timeout=float(timeout) + 15,
        )

    def exists(self, sandbox: Any, path: str) -> bool:
        return bool(
            self._call(sandbox.fs.exists, path, _wait_timeout=15)
        )

    def stat(self, sandbox: Any, path: str) -> Any:
        return self._call(sandbox.fs.stat, path, _wait_timeout=15)

    def list(self, sandbox: Any, path: str) -> list[Any]:
        return list(self._call(sandbox.fs.list, path, _wait_timeout=30))

    def read_limited(
        self, sandbox: Any, path: str, max_bytes: int
    ) -> tuple[bytes, bool]:
        async def _read_limited() -> tuple[bytes, bool]:
            stream = await sandbox.fs.read_stream(path)
            payload = bytearray()
            limit = max_bytes + 1
            async for chunk in stream:
                remaining = limit - len(payload)
                if remaining <= 0:
                    break
                payload.extend(bytes(chunk)[:remaining])
                if len(payload) >= limit:
                    break
            return bytes(payload[:max_bytes]), len(payload) > max_bytes

        return self._bridge.call(_read_limited(), timeout=30)

    def write(self, sandbox: Any, path: str, data: bytes) -> None:
        self._call(sandbox.fs.write, path, data, _wait_timeout=30)

    def rename(self, sandbox: Any, source: str, destination: str) -> None:
        self._call(
            sandbox.fs.rename,
            source,
            destination,
            _wait_timeout=30,
        )

    def remove(self, sandbox: Any, path: str) -> None:
        self._call(sandbox.fs.remove, path, _wait_timeout=30)

    def mkdir_parents(self, sandbox: Any, path: str) -> None:
        self.shell(
            sandbox,
            f"mkdir -p -- {shlex.quote(path)}",
            cwd=GUEST_WORKSPACE,
            timeout=30,
        )

    def copy_from_host(self, sandbox: Any, host: Path, guest: str) -> None:
        self._call(
            sandbox.fs.copy_from_host,
            str(host),
            guest,
            _wait_timeout=120,
        )

    def copy_to_host(self, sandbox: Any, guest: str, host: Path) -> None:
        self._call(
            sandbox.fs.copy_to_host,
            guest,
            str(host),
            _wait_timeout=120,
        )

    def close(self) -> None:
        self._bridge.close()


@dataclass(slots=True)
class _SessionSandbox:
    sandbox: Any
    name: str
    volume_name: str
    host_workspace: Path | None
    cwd: str = GUEST_WORKSPACE
    lock: threading.RLock = field(default_factory=threading.RLock)


class SandboxManager:
    """Own one long-lived microVM per SJTUClaw session."""

    def __init__(
        self,
        config: SandboxConfig,
        *,
        backend: SandboxBackend | None = None,
    ) -> None:
        self.config = config
        self._backend = backend
        self._sessions: dict[str, _SessionSandbox] = {}
        self._session_enabled: dict[str, bool] = {}
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.RLock] = {}
        self._agent_backend_provider: Callable[[str], str] | None = None
        self._session_state_loader: Callable[[str], bool | None] | None = None
        self._session_state_saver: Callable[[str, bool], None] | None = None
        self._availability: bool | None = None
        install_seed = str(DATA_DIR.resolve()).encode("utf-8", errors="replace")
        self._install_id = hashlib.sha256(install_seed).hexdigest()[:10]

    @staticmethod
    def sdk_available() -> bool:
        """Probe the SDK, bundled runtime and local hypervisor once per manager."""
        try:
            if importlib.util.find_spec("microsandbox") is None:
                return False
            from microsandbox._runtime import msb_path

            override = os.getenv("MSB_PATH", "").strip()
            executable = (
                Path(override).expanduser()
                if override
                else msb_path()
            )
            if not executable.is_file():
                return False
            if os.name == "nt" and is_frozen() and not override:
                executable = _prepare_windows_gui_msb(executable)
            completed = subprocess.run(
                [str(executable), "doctor"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return completed.returncode == 0
        except (ImportError, OSError, subprocess.SubprocessError, ValueError):
            return False

    @property
    def available(self) -> bool:
        """Return whether the microsandbox SDK/runtime can be used."""
        with self._lock:
            if self._backend is not None:
                return True
            if self._availability is None:
                self._availability = self.sdk_available()
            return self._availability

    @property
    def active(self) -> bool:
        """Return whether the configured default can use microsandbox."""
        return self.config.enabled and self.available

    @property
    def required(self) -> bool:
        return self.config.mode == "required"

    def is_session_enabled(self, session_id: str) -> bool:
        """Return the session's explicit state or the configured default."""
        preference = self.session_preference(session_id)
        if self.required:
            return True
        return (
            preference
            if preference is not None
            else self.config.enabled
        )

    def session_preference(self, session_id: str) -> bool | None:
        """Return the explicit persisted state, if this session has one."""
        with self._lock:
            if session_id in self._session_enabled:
                return self._session_enabled[session_id]
            loader = self._session_state_loader
        if loader is None:
            return None
        try:
            preference = loader(session_id)
        except Exception as exc:
            raise SandboxError(
                f"读取 session sandbox 状态失败: {exc}"
            ) from exc
        if preference is None:
            return None
        if not isinstance(preference, bool):
            raise SandboxError("持久化的 session sandbox 状态不是布尔值")
        with self._lock:
            self._session_enabled.setdefault(session_id, preference)
            return self._session_enabled[session_id]

    def is_session_explicitly_enabled(self, session_id: str) -> bool:
        """Return whether the user explicitly enabled this session."""
        return self.session_preference(session_id) is True

    def set_session_state_store(
        self,
        *,
        loader: Callable[[str], bool | None] | None,
        saver: Callable[[str, bool], None] | None,
    ) -> None:
        """Configure persistence hooks for per-session sandbox preferences."""
        with self._lock:
            self._session_state_loader = loader
            self._session_state_saver = saver
            self._session_enabled.clear()

    def _persist_session_preference(
        self,
        session_id: str,
        enabled: bool,
    ) -> None:
        with self._lock:
            saver = self._session_state_saver
        if saver is None:
            return
        try:
            saver(session_id, enabled)
        except Exception as exc:
            raise SandboxError(
                f"保存 session sandbox 状态失败: {exc}"
            ) from exc

    def set_session_enabled(
        self,
        session_id: str,
        enabled: bool,
        workspace_manager: Any,
    ) -> None:
        """Enable or disable sandbox routing for exactly one session."""
        if enabled:
            agent_backend = self._agent_backend(session_id)
            if agent_backend != "sjtuclaw":
                raise SandboxError(
                    "sandbox 当前只覆盖 SJTUClaw 原生后端；"
                    f"当前后端 {agent_backend} 尚未纳入 microVM。"
                )
            if workspace_manager.is_unlimited(session_id):
                raise SandboxError(
                    "sandbox 与 UNLIMITED 不兼容；"
                    "请先使用 /unlimited off。"
                )
            if not self.available:
                raise SandboxError(
                    "microsandbox SDK/运行时不可用；"
                    "请先安装 sandbox 可选依赖并检查虚拟化环境。"
                )
            self._persist_session_preference(session_id, True)
            with self._lock:
                self._session_enabled[session_id] = True
            return

        if self.required:
            raise SandboxError(
                "SANDBOX_MODE=required，不允许关闭当前 session 的 sandbox。"
            )
        # Stop first so a failure cannot report the session as disabled while
        # its old microVM is still alive.
        self.close_session(session_id)
        self._persist_session_preference(session_id, False)
        with self._lock:
            self._session_enabled[session_id] = False

    def is_session_effective(
        self,
        session_id: str,
        workspace_manager: Any,
    ) -> bool:
        """Return whether this session is currently routed to a microVM."""
        return (
            self.is_session_enabled(session_id)
            and self._agent_backend(session_id) == "sjtuclaw"
            and not workspace_manager.is_unlimited(session_id)
            and self.available
        )

    def set_agent_backend_provider(
        self, provider: Callable[[str], str] | None
    ) -> None:
        """Tell tool routing whether a session uses the native Agent backend."""
        self._agent_backend_provider = provider

    def _agent_backend(self, session_id: str) -> str:
        if self._agent_backend_provider is None:
            return "sjtuclaw"
        try:
            return self._agent_backend_provider(session_id)
        except Exception:
            # Failure to establish the backend must not weaken required mode.
            return "unknown"

    def should_use(self, session_id: str, workspace_manager: Any) -> bool:
        """Return whether native tools must route through microsandbox."""
        preference = self.session_preference(session_id)
        explicitly_enabled = preference is True
        enabled = (
            True
            if self.required
            else preference
            if preference is not None
            else self.config.enabled
        )
        if not enabled:
            return False
        agent_backend = self._agent_backend(session_id)
        if agent_backend != "sjtuclaw":
            if self.required or explicitly_enabled:
                raise SandboxError(
                    "当前 session 已要求使用 sandbox，但 sandbox "
                    "仅覆盖 SJTUClaw 原生后端；"
                    f"当前后端 {agent_backend} 尚未纳入 microVM。"
                )
            return False
        if workspace_manager.is_unlimited(session_id):
            if self.required or explicitly_enabled:
                raise SandboxError(
                    "当前 session 已要求使用 sandbox，不能与 UNLIMITED 共用；"
                    "请先关闭 /unlimited。"
                )
            return False
        if self.available:
            return True
        if self.required or explicitly_enabled:
            raise SandboxError(
                "当前 session 已要求使用 sandbox，但 "
                "microsandbox SDK/运行时不可用；"
                "已拒绝回退到宿主执行。"
            )
        return False

    def status(self, session_id: str, workspace_manager: Any) -> dict[str, Any]:
        with self._lock:
            live = session_id in self._sessions
        host = workspace_manager.get(session_id)
        agent_backend = self._agent_backend(session_id)
        preference = self.session_preference(session_id)
        enabled = self.is_session_enabled(session_id)
        available = self.available
        return {
            "mode": self.config.mode,
            "enabled": enabled,
            "preference": preference,
            "effective": (
                enabled
                and available
                and agent_backend == "sjtuclaw"
                and not workspace_manager.is_unlimited(session_id)
            ),
            "available": available,
            "running": live,
            "agentBackend": agent_backend,
            "covered": agent_backend == "sjtuclaw",
            "workspaceKind": "host-mounted" if host is not None else "sandbox-private",
            "hostWorkspace": str(host) if host is not None else None,
            "guestWorkspace": GUEST_WORKSPACE,
            "projectVenv": (
                GUEST_PROJECT_VENV if self.config.project_venv else None
            ),
            "pipIndexUrl": self.config.pip_index_url or None,
            "image": self.config.image,
            "network": self.config.network,
            "security": self.config.security,
            "statVirtualization": self.config.stat_virtualization,
        }

    def _backend_instance(self) -> SandboxBackend:
        with self._lock:
            if self._backend is None:
                self._backend = MicrosandboxBackend()
            return self._backend

    def _session_lock(self, session_id: str) -> threading.RLock:
        with self._lock:
            return self._session_locks.setdefault(
                session_id, threading.RLock()
            )

    def _names(self, session_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:18]
        prefix = f"sjtuclaw-{self._install_id}-{digest}"
        # A CLI and Gateway may share the same session store. Process-scoped
        # VM names prevent one process's replace=True startup from killing the
        # other's live VM; the deterministic volume remains session-scoped.
        return f"{prefix}-{os.getpid()}", f"{prefix}-workspace"

    def _bootstrap_project_venv(
        self,
        backend: SandboxBackend,
        session: _SessionSandbox,
    ) -> None:
        """Create a runtime venv backed by a persistent dependency store.

        A CPython venv stored directly on a microsandbox Windows passthrough
        volume cannot be reopened reliably after a microVM restart (its
        ``pyvenv.cfg`` returns EACCES). Keep the executable venv on the Linux
        rootfs and persist only project packages/scripts under
        ``/workspace/.venv``. A small sync helper restores packages into the
        runtime venv on boot and saves changes after shell commands, keeping
        normal ``python`` and ``pip`` behavior (including console scripts).
        """
        if not self.config.project_venv:
            return
        project = shlex.quote(GUEST_PROJECT_VENV)
        runtime = shlex.quote(GUEST_RUNTIME_VENV)
        command = (
            "# SJTUCLAW_PROJECT_VENV_BOOTSTRAP\n"
            "set -eu\n"
            f"project={project}\n"
            f"runtime={runtime}\n"
            'marker="$project/.sjtuclaw-managed"\n'
            'if [ -d "$project" ] && [ ! -f "$marker" ] '
            '&& [ -n "$(ls -A "$project" 2>/dev/null)" ]; then\n'
            '  echo "现有 /workspace/.venv 不是 SJTUClaw 管理的项目依赖'
            '目录；请移动、删除或重命名后重试。" >&2\n'
            "  exit 73\n"
            "fi\n"
            'if [ -f "$marker" ] '
            '&& ! grep -qx "layout=sync-v1" "$marker"; then\n'
            '  echo "现有 /workspace/.venv 使用了不兼容的 SJTUClaw '
            '布局；请删除或重命名后重试。" >&2\n'
            "  exit 73\n"
            "fi\n"
            'if [ -f "$marker" ]; then\n'
            # Refresh the Windows passthrough backend's per-VM metadata view
            # before Python imports files persisted by the previous VM.
            '  chmod -R u+rwX,go+rX "$project"\n'
            "fi\n"
            'python_bin="$(command -v python3 || command -v python || true)"\n'
            'if [ -z "$python_bin" ]; then\n'
            '  echo "当前 sandbox 镜像没有 Python，无法创建项目 .venv。" >&2\n'
            "  exit 72\n"
            "fi\n"
            'rm -rf "$runtime"\n'
            'mkdir -p "$(dirname "$runtime")" "$project/bin"\n'
            '"$python_bin" -m venv --without-pip '
            '--system-site-packages "$runtime"\n'
            'for pip_name in pip pip3; do\n'
            '  printf \'#!/bin/sh\\nexec "$(dirname "$0")/python" '
            '-m pip "$@"\\n\' > "$runtime/bin/$pip_name"\n'
            '  chmod 755 "$runtime/bin/$pip_name"\n'
            "done\n"
            f'"$runtime/bin/python" {_GUEST_PROJECT_ENV_SYNC} '
            'restore "$project" "$runtime"\n'
            'printf "layout=sync-v1\\n" > "$marker"\n'
            '"$runtime/bin/python" -m pip --version >/dev/null\n'
        )
        try:
            prepared = backend.shell(
                session.sandbox,
                "mkdir -p -- /opt/sjtuclaw",
                cwd=GUEST_WORKSPACE,
                timeout=30,
            )
            if int(getattr(prepared, "exit_code", 1)) != 0:
                raise SandboxError("无法创建 sandbox 项目环境运行目录")
            helper_source = (
                Path(__file__)
                .with_name("project_env_sync.py")
                .read_bytes()
            )
            backend.write(
                session.sandbox,
                _GUEST_PROJECT_ENV_SYNC,
                helper_source,
            )
            output = backend.shell(
                session.sandbox,
                command,
                cwd=GUEST_WORKSPACE,
                timeout=180,
            )
        except Exception as exc:
            raise SandboxError(f"初始化项目 Python .venv 失败: {exc}") from exc
        exit_code = int(getattr(output, "exit_code", 1))
        if exit_code == 0 and not bool(
            getattr(output, "output_limited", False)
        ):
            return
        stderr = bytes(getattr(output, "stderr_bytes", b"")).decode(
            "utf-8",
            errors="replace",
        ).strip()
        detail = stderr or f"命令退出码 {exit_code}"
        raise SandboxError(f"初始化项目 Python .venv 失败: {detail}")

    def _project_environment(self) -> str:
        """Return POSIX exports applied to every sandbox shell command."""
        lines = ["export PIP_DISABLE_PIP_VERSION_CHECK=1"]
        if self.config.project_venv:
            lines.extend(
                [
                    f"export VIRTUAL_ENV={shlex.quote(GUEST_RUNTIME_VENV)}",
                    "export SJTUCLAW_PROJECT_ENV="
                    f"{shlex.quote(GUEST_PROJECT_VENV)}",
                    'unset PIP_PREFIX PIP_TARGET PYTHONUSERBASE',
                    'export PATH="$VIRTUAL_ENV/bin:$PATH"',
                ]
            )
        if self.config.pip_index_url:
            lines.append(
                "export PIP_INDEX_URL="
                f"{shlex.quote(self.config.pip_index_url)}"
            )
        return "\n".join(lines)

    def _ensure(self, session_id: str, workspace_manager: Any) -> _SessionSandbox:
        if not self.should_use(session_id, workspace_manager):
            raise SandboxError("当前 session 未启用 sandbox")
        host = workspace_manager.get(session_id)
        host = host.resolve() if host is not None else None
        with self._session_lock(session_id):
            with self._lock:
                current = self._sessions.get(session_id)
            if current is not None and current.host_workspace == host:
                if self._backend_instance().alive(current.sandbox):
                    return current
            # Serialize creation for this session while allowing other sessions
            # to start and execute independently.
            backend = self._backend_instance()
            if current is not None:
                try:
                    backend.stop(current.sandbox, current.name)
                except Exception as exc:
                    raise SandboxError(
                        f"旧 sandbox 停止失败，未创建替代环境: {exc}"
                    ) from exc
                with self._lock:
                    if self._sessions.get(session_id) is current:
                        self._sessions.pop(session_id, None)
            name, volume_name = self._names(session_id)
            sandbox = backend.create(
                name=name,
                volume_name=volume_name,
                host_workspace=host,
                config=self.config,
            )
            created = _SessionSandbox(
                sandbox=sandbox,
                name=name,
                volume_name=volume_name,
                host_workspace=host,
            )
            try:
                self._bootstrap_project_venv(backend, created)
            except Exception:
                try:
                    backend.stop(created.sandbox, created.name)
                except Exception:
                    logger.exception(
                        "项目 .venv 初始化失败后停止 sandbox 失败: %s",
                        created.name,
                    )
                raise
            with self._lock:
                self._sessions[session_id] = created
            return created

    @staticmethod
    def guest_path(path: str, *, cwd: str = GUEST_WORKSPACE) -> str:
        """Resolve a structured-tool path and keep it inside /workspace."""
        raw = str(path).strip()
        if not raw:
            raise SandboxError("路径不能为空")
        if _WINDOWS_DRIVE_RE.match(raw) or raw.startswith("\\\\"):
            raise SandboxError(
                f"拒绝直接访问宿主绝对路径: \"{path}\"。"
                "请先绑定 workspace 或上传/复制文件。"
            )
        raw = raw.replace("\\", "/")
        candidate = (
            posixpath.normpath(raw)
            if raw.startswith("/")
            else posixpath.normpath(posixpath.join(cwd, raw))
        )
        try:
            PurePosixPath(candidate).relative_to(PurePosixPath(GUEST_WORKSPACE))
        except ValueError as exc:
            raise SandboxError(
                f"路径超出 sandbox workspace: \"{path}\"。"
                "结构化文件工具仅能访问 /workspace；"
                "Shell 可在 microVM 内使用其他路径。"
            ) from exc
        return candidate

    @staticmethod
    def _safe_export_name(guest_path: str) -> str:
        name = PurePosixPath(guest_path).name
        name = _INVALID_EXPORT_CHARS_RE.sub("_", name).rstrip(". ")
        if not name:
            return "download"
        stem = PurePosixPath(name).stem.upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            name = "_" + name
        if len(name) <= 180:
            return name
        suffix = PurePosixPath(name).suffix[:32]
        stem_budget = max(1, 180 - len(suffix))
        return name[:stem_budget] + suffix

    def new_shell(
        self,
        session_id: str,
        workspace_manager: Any,
        sub_dir: str = "",
    ) -> dict[str, str]:
        session = self._ensure(session_id, workspace_manager)
        cwd = self.guest_path(sub_dir) if sub_dir else GUEST_WORKSPACE
        backend = self._backend_instance()
        if not backend.exists(session.sandbox, cwd):
            raise SandboxError(f"sub_dir 不是目录或不存在: \"{sub_dir}\"")
        metadata = backend.stat(session.sandbox, cwd)
        if str(getattr(metadata, "kind", "")) not in {"directory", "dir"}:
            raise SandboxError(f"sub_dir 不是目录或不存在: \"{sub_dir}\"")
        with session.lock:
            session.cwd = cwd
        return {
            "workspace": GUEST_WORKSPACE,
            "cwd": cwd,
            "shell": "/bin/sh (microsandbox)",
            "python": (
                f"{GUEST_RUNTIME_VENV}/bin/python"
                if self.config.project_venv
                else "image default"
            ),
            "projectVenv": (
                GUEST_PROJECT_VENV if self.config.project_venv else None
            ),
            "workspaceKind": (
                "host-mounted"
                if session.host_workspace is not None
                else "sandbox-private"
            ),
        }

    def run_command(
        self,
        session_id: str,
        workspace_manager: Any,
        command: str,
        timeout: int,
    ) -> SandboxCommandResult:
        session = self._ensure(session_id, workspace_manager)
        marker = f"__SJTUCLAW_CWD_{uuid.uuid4().hex}__"
        with session.lock:
            script = (
                f"{self._project_environment()}\n"
                f"{command}\n"
                "__sjtuclaw_code=$?\n"
                + (
                    f'"$VIRTUAL_ENV/bin/python" {_GUEST_PROJECT_ENV_SYNC} '
                    'save "$SJTUCLAW_PROJECT_ENV" "$VIRTUAL_ENV"\n'
                    "__sjtuclaw_sync_code=$?\n"
                    'if [ "$__sjtuclaw_code" -eq 0 ] '
                    '&& [ "$__sjtuclaw_sync_code" -ne 0 ]; then\n'
                    "  __sjtuclaw_code=$__sjtuclaw_sync_code\n"
                    "fi\n"
                    if self.config.project_venv
                    else ""
                )
                + f"printf '\\n{marker}%s\\n' \"$PWD\"\n"
                "exit $__sjtuclaw_code"
            )
            try:
                output = self._backend_instance().shell(
                    session.sandbox,
                    script,
                    cwd=session.cwd,
                    timeout=timeout,
                )
            except TimeoutError:
                return SandboxCommandResult(
                    exit_code=-1,
                    stdout="",
                    stderr="",
                    cwd=session.cwd,
                    timed_out=True,
                )
            except Exception as exc:
                raise SandboxError(f"sandbox 命令执行失败: {exc}") from exc

            stdout_bytes = bytes(getattr(output, "stdout_bytes", b""))
            stderr_bytes = bytes(getattr(output, "stderr_bytes", b""))
            stdout_tail = bytes(
                getattr(output, "stdout_tail_bytes", stdout_bytes[-_STREAM_TAIL_BYTES:])
            )
            marker_bytes = marker.encode("ascii")
            marker_index = stdout_bytes.rfind(marker_bytes)
            if marker_index >= 0:
                cwd_payload = stdout_bytes[marker_index + len(marker_bytes):]
                stdout_bytes = stdout_bytes[:marker_index].rstrip(b"\r\n")
            else:
                tail_index = stdout_tail.rfind(marker_bytes)
                cwd_payload = (
                    stdout_tail[tail_index + len(marker_bytes):]
                    if tail_index >= 0
                    else b""
                )
            if cwd_payload:
                cwd_line = cwd_payload.splitlines()[0].decode(
                    "utf-8", errors="replace"
                )
                if cwd_line.startswith("/"):
                    session.cwd = posixpath.normpath(cwd_line)
            return SandboxCommandResult(
                exit_code=int(getattr(output, "exit_code", 1)),
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                cwd=session.cwd,
                stdout_truncated=bool(
                    getattr(output, "stdout_truncated", False)
                ),
                stderr_truncated=bool(
                    getattr(output, "stderr_truncated", False)
                ),
                output_limited=bool(getattr(output, "output_limited", False)),
            )

    def list_dir(
        self,
        session_id: str,
        workspace_manager: Any,
        path: str,
    ) -> list[SandboxEntry]:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(path)
        try:
            raw_entries = self._backend_instance().list(session.sandbox, guest)
        except Exception as exc:
            raise SandboxError(f"cannot read directory \"{path}\": {exc}") from exc
        entries = [
            SandboxEntry(
                name=PurePosixPath(str(entry.path)).name,
                kind=str(entry.kind),
                size=int(getattr(entry, "size", 0)),
            )
            for entry in raw_entries
        ]
        return sorted(entries, key=lambda entry: entry.name.casefold())

    def read_file(
        self,
        session_id: str,
        workspace_manager: Any,
        path: str,
        *,
        max_bytes: int = _MAX_OUTPUT_BYTES,
    ) -> tuple[bytes, bool]:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(path)
        try:
            return self._backend_instance().read_limited(
                session.sandbox,
                guest,
                max_bytes,
            )
        except Exception as exc:
            raise SandboxError(f"cannot read file \"{path}\": {exc}") from exc

    @staticmethod
    def _temporary_guest_path(guest_path: str) -> str:
        parent = posixpath.dirname(guest_path)
        name = PurePosixPath(guest_path).name
        return posixpath.join(
            parent,
            f".{name}.sjtuclaw-{uuid.uuid4().hex}.tmp",
        )

    @staticmethod
    def _remove_temporary(
        backend: SandboxBackend,
        session: _SessionSandbox,
        temporary: str,
    ) -> None:
        try:
            backend.remove(session.sandbox, temporary)
        except Exception:
            logger.warning(
                "清理 sandbox 临时文件失败: %s",
                temporary,
                exc_info=True,
            )

    def _atomic_write(
        self,
        backend: SandboxBackend,
        session: _SessionSandbox,
        guest: str,
        payload: bytes,
    ) -> None:
        temporary = self._temporary_guest_path(guest)
        backend.mkdir_parents(session.sandbox, posixpath.dirname(guest))
        try:
            backend.write(session.sandbox, temporary, payload)
            backend.rename(session.sandbox, temporary, guest)
        except Exception:
            self._remove_temporary(backend, session, temporary)
            raise

    def create_file(
        self, session_id: str, workspace_manager: Any, path: str
    ) -> None:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(path)
        backend = self._backend_instance()
        with session.lock:
            if backend.exists(session.sandbox, guest):
                raise FileExistsError(path)
            self._atomic_write(backend, session, guest, b"")

    def overwrite_file(
        self,
        session_id: str,
        workspace_manager: Any,
        path: str,
        content: str,
    ) -> None:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(path)
        backend = self._backend_instance()
        with session.lock:
            self._atomic_write(
                backend,
                session,
                guest,
                content.encode("utf-8"),
            )

    def edit_file(
        self,
        session_id: str,
        workspace_manager: Any,
        path: str,
        old: str,
        new: str,
    ) -> None:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(path)
        backend = self._backend_instance()
        with session.lock:
            try:
                payload, truncated = backend.read_limited(
                    session.sandbox,
                    guest,
                    16 * 1024 * 1024,
                )
            except Exception as exc:
                raise SandboxError(
                    f"edit_file 失败 \"{path}\": 无法读取文件: {exc}"
                ) from exc
            if truncated:
                raise SandboxError(f"edit_file 失败 \"{path}\": 文件过大")
            try:
                original = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SandboxError(
                    f"edit_file 失败 \"{path}\": 文件不是 UTF-8 文本"
                ) from exc
            count = original.count(old)
            if count == 0:
                raise SandboxError(
                    f"edit_file 失败 \"{path}\": 未找到要替换的内容"
                )
            if count > 1:
                raise SandboxError(
                    f"edit_file 失败 \"{path}\": 要替换的内容出现了 {count} 次，"
                    "请提供更精确的匹配字符串以确保唯一匹配"
                )
            self._atomic_write(
                backend,
                session,
                guest,
                original.replace(old, new, 1).encode("utf-8"),
            )

    def import_file(
        self,
        session_id: str,
        workspace_manager: Any,
        host_path: Path,
        guest_path: str,
    ) -> None:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(guest_path)
        backend = self._backend_instance()
        with session.lock:
            temporary = self._temporary_guest_path(guest)
            backend.mkdir_parents(session.sandbox, posixpath.dirname(guest))
            try:
                backend.copy_from_host(
                    session.sandbox,
                    host_path,
                    temporary,
                )
                backend.rename(session.sandbox, temporary, guest)
            except Exception:
                self._remove_temporary(backend, session, temporary)
                raise

    def export_file(
        self,
        session_id: str,
        workspace_manager: Any,
        guest_path: str,
    ) -> Path:
        session = self._ensure(session_id, workspace_manager)
        guest = self.guest_path(guest_path)
        backend = self._backend_instance()
        metadata = backend.stat(session.sandbox, guest)
        if str(getattr(metadata, "kind", "")) not in {"file", "regular"}:
            raise SandboxError(f"create_download 失败：路径不是文件 \"{guest_path}\"")
        safe_name = self._safe_export_name(guest)
        export_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]
        export_dir = (
            DATA_DIR
            / "sandbox"
            / "exports"
            / export_key
            / uuid.uuid4().hex[:12]
        )
        export_dir.mkdir(parents=True, exist_ok=True)
        destination = export_dir / safe_name
        try:
            backend.copy_to_host(session.sandbox, guest, destination)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return destination

    def close_session(self, session_id: str) -> None:
        with self._session_lock(session_id):
            with self._lock:
                session = self._sessions.get(session_id)
                backend = self._backend
            if session is None:
                return
            if backend is None:
                raise SandboxError(
                    "sandbox 管理器状态异常：运行中的 session 没有后端"
                )
            try:
                backend.stop(session.sandbox, session.name)
            except Exception as exc:
                raise SandboxError(
                    f"停止 sandbox {session.name} 失败: {exc}"
                ) from exc
            with self._lock:
                if self._sessions.get(session_id) is session:
                    self._sessions.pop(session_id, None)

    def purge_session(self, session_id: str) -> None:
        with self._session_lock(session_id):
            errors: list[str] = []
            try:
                self.close_session(session_id)
            except Exception as exc:
                errors.append(str(exc))
            with self._lock:
                backend = self._backend
            _, volume_name = self._names(session_id)
            # Cleanup is independent from the configured default. A session
            # may have created a private volume through an explicit
            # ``/sandbox on`` while SANDBOX_MODE=off, then been deleted after
            # an application restart.
            if backend is None and self.available:
                try:
                    backend = self._backend_instance()
                except Exception as exc:
                    errors.append(f"初始化 sandbox 清理后端失败: {exc}")
            if backend is not None:
                try:
                    backend.remove_volume(volume_name)
                except Exception as exc:
                    errors.append(f"删除 sandbox volume {volume_name} 失败: {exc}")
            with self._lock:
                self._session_enabled.pop(session_id, None)
            if errors:
                raise SandboxError("；".join(errors))

    def close_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._session_enabled.clear()
            self._session_locks.clear()
            backend = self._backend
            self._backend = None
        if backend is None:
            return
        for session in sessions:
            try:
                backend.stop(session.sandbox, session.name)
            except Exception:
                logger.exception(
                    "应用退出时停止 sandbox 失败: %s",
                    session.name,
                )
        try:
            backend.close()
        except Exception:
            logger.exception("应用退出时关闭 microsandbox 后端失败")
