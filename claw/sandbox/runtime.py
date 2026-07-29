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
import subprocess
import threading
import uuid
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Coroutine, Protocol

from claw.config import DATA_DIR
from claw.sandbox.config import SandboxConfig

logger = logging.getLogger(__name__)

GUEST_WORKSPACE = "/workspace"
_MAX_OUTPUT_BYTES = 64 * 1024
_STREAM_CAPTURE_BYTES = _MAX_OUTPUT_BYTES + 4 * 1024
_STREAM_TAIL_BYTES = 4 * 1024
_MAX_STREAM_BYTES = 8 * 1024 * 1024
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_INVALID_EXPORT_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


class SandboxError(RuntimeError):
    """A user-actionable sandbox error."""


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
        if host_workspace is None:
            mount = msb.MountConfig(
                kind=msb.MountKind.NAMED,
                named=volume_name,
                named_mode="ensure-exists",
                named_kind="dir",
                quota_mib=config.workspace_quota_mib,
                nosuid=True,
                nodev=True,
                stat_virtualization=msb.StatVirtualization.STRICT,
                host_permissions=msb.HostPermissions.PRIVATE,
            )
        else:
            mount = msb.MountConfig(
                kind=msb.MountKind.BIND,
                bind=str(host_workspace),
                quota_mib=config.workspace_quota_mib,
                nosuid=True,
                nodev=True,
                stat_virtualization=msb.StatVirtualization.STRICT,
                host_permissions=msb.HostPermissions.PRIVATE,
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

            executable = msb_path()
            if not executable.is_file():
                return False
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
        with self._lock:
            return self._session_enabled.get(
                session_id,
                self.config.enabled,
            )

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
        if not self.is_session_enabled(session_id):
            return False
        agent_backend = self._agent_backend(session_id)
        if agent_backend != "sjtuclaw":
            if self.required:
                raise SandboxError(
                    "required sandbox 模式仅允许 SJTUClaw 原生后端；"
                    f"当前后端 {agent_backend} 尚未纳入 microVM。"
                )
            return False
        if workspace_manager.is_unlimited(session_id):
            if self.required:
                raise SandboxError(
                    "required sandbox 模式与 UNLIMITED 不兼容；"
                    "请先关闭 /unlimited。"
                )
            return False
        if self.available:
            return True
        if self.required:
            raise SandboxError(
                "SANDBOX_MODE=required，但 microsandbox SDK/运行时不可用；"
                "已拒绝回退到宿主执行。"
            )
        return False

    def status(self, session_id: str, workspace_manager: Any) -> dict[str, Any]:
        with self._lock:
            live = session_id in self._sessions
        host = workspace_manager.get(session_id)
        agent_backend = self._agent_backend(session_id)
        enabled = self.is_session_enabled(session_id)
        available = self.available
        return {
            "mode": self.config.mode,
            "enabled": enabled,
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
            "image": self.config.image,
            "network": self.config.network,
            "security": self.config.security,
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
                f"{command}\n"
                "__sjtuclaw_code=$?\n"
                f"printf '\\n{marker}%s\\n' \"$PWD\"\n"
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
