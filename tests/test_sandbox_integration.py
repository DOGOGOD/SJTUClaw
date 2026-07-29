from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from claw.sandbox import SandboxConfig, SandboxError, SandboxManager
from claw.sandbox.runtime import (
    GUEST_WORKSPACE,
    MicrosandboxBackend,
    _AsyncBridge,
    _BoundedCapture,
)
from claw.session.store import (
    AUTO_MODE_METADATA_KEY,
    SANDBOX_MODE_METADATA_KEY,
    SessionStore,
)
from claw.tools import register_all_tools
from claw.tools.base import ToolRegistry
from claw.tools.base import is_workspace_violation


class FakeWorkspaceManager:
    def __init__(self, workspace: Path | None = None, *, unlimited: bool = False):
        self.workspace = workspace
        self.unlimited = unlimited

    def get(self, _session_id: str) -> Path | None:
        return self.workspace

    def is_unlimited(self, _session_id: str) -> bool:
        return self.unlimited


class FakeBackend:
    def __init__(self):
        self.created: list[dict] = []
        self.stopped: list[str] = []
        self.removed_volumes: list[str] = []
        self.shell_commands: list[str] = []
        self.closed = False
        self.alive_value = True
        self.read_limits: list[int] = []
        self._stores: dict[str, dict[str, bytes]] = {}
        self._dirs: dict[str, set[str]] = {}

    def create(self, **kwargs):
        self.created.append(kwargs)
        volume_name = kwargs["volume_name"]
        self._stores.setdefault(volume_name, {})
        self._dirs.setdefault(volume_name, {"/workspace"})
        return SimpleNamespace(volume_name=volume_name)

    def stop(self, _sandbox, name):
        self.stopped.append(name)

    def remove_volume(self, volume_name):
        self.removed_volumes.append(volume_name)
        self._stores.pop(volume_name, None)
        self._dirs.pop(volume_name, None)

    def alive(self, _sandbox):
        return self.alive_value

    def shell(self, sandbox, command, *, cwd, timeout):
        del timeout
        self.shell_commands.append(command)
        marker_match = re.search(r"(__SJTUCLAW_CWD_[0-9a-f]+__)", command)
        if command.startswith("mkdir -p -- "):
            target = command.removeprefix("mkdir -p -- ").strip().strip("'")
            self.mkdir_parents(sandbox, target)
            return SimpleNamespace(
                exit_code=0,
                stdout_bytes=b"",
                stderr_bytes=b"",
                stdout_tail_bytes=b"",
                stdout_truncated=False,
                stderr_truncated=False,
                output_limited=False,
            )
        next_cwd = cwd
        cd_match = re.search(r"(?:^|\n)cd\s+([^\n;]+)", command)
        if cd_match:
            raw = cd_match.group(1).strip().strip("'\"")
            next_cwd = (
                raw
                if raw.startswith("/")
                else str(PurePosixPath(cwd) / raw)
            )
        user_output = "sandbox-output"
        if marker_match:
            user_output += f"\n{marker_match.group(1)}{next_cwd}\n"
        stdout_bytes = user_output.encode("utf-8")
        return SimpleNamespace(
            exit_code=0,
            stdout_bytes=stdout_bytes,
            stderr_bytes=b"",
            stdout_tail_bytes=stdout_bytes[-4096:],
            stdout_truncated=False,
            stderr_truncated=False,
            output_limited=False,
        )

    def _files(self, sandbox):
        return self._stores[sandbox.volume_name]

    def _directories(self, sandbox):
        return self._dirs[sandbox.volume_name]

    def exists(self, sandbox, path):
        return path in self._files(sandbox) or path in self._directories(sandbox)

    def stat(self, sandbox, path):
        if path in self._directories(sandbox):
            return SimpleNamespace(kind="directory", size=0)
        if path in self._files(sandbox):
            return SimpleNamespace(kind="file", size=len(self._files(sandbox)[path]))
        raise FileNotFoundError(path)

    def list(self, sandbox, path):
        prefix = path.rstrip("/") + "/"
        entries = {}
        for directory in self._directories(sandbox):
            if directory.startswith(prefix):
                rest = directory[len(prefix):]
                if rest and "/" not in rest:
                    entries[rest] = SimpleNamespace(
                        path=f"{prefix}{rest}",
                        kind="directory",
                        size=0,
                    )
        for file_path, payload in self._files(sandbox).items():
            if file_path.startswith(prefix):
                rest = file_path[len(prefix):]
                if rest and "/" not in rest:
                    entries[rest] = SimpleNamespace(
                        path=f"{prefix}{rest}",
                        kind="file",
                        size=len(payload),
                    )
        return list(entries.values())

    def read_limited(self, sandbox, path, max_bytes):
        self.read_limits.append(max_bytes)
        payload = self._files(sandbox)[path]
        return payload[:max_bytes], len(payload) > max_bytes

    def write(self, sandbox, path, data):
        self._files(sandbox)[path] = data

    def rename(self, sandbox, source, destination):
        self._files(sandbox)[destination] = self._files(sandbox).pop(source)

    def remove(self, sandbox, path):
        self._files(sandbox).pop(path, None)

    def mkdir_parents(self, sandbox, path):
        current = PurePosixPath("/")
        for part in PurePosixPath(path).parts[1:]:
            current /= part
            self._directories(sandbox).add(str(current))

    def copy_from_host(self, sandbox, host, guest):
        self._files(sandbox)[guest] = host.read_bytes()

    def copy_to_host(self, sandbox, guest, host):
        host.parent.mkdir(parents=True, exist_ok=True)
        host.write_bytes(self._files(sandbox)[guest])

    def close(self):
        self.closed = True


def make_manager(backend: FakeBackend, *, mode: str = "required") -> SandboxManager:
    return SandboxManager(
        SandboxConfig(mode=mode, image="test-image"),
        backend=backend,
    )


def test_sandbox_config_rejects_invalid_resource_values(monkeypatch):
    import claw.sandbox.config as sandbox_config

    values = {"SANDBOX_MODE": "required", "SANDBOX_CPUS": "999"}
    monkeypatch.setattr(
        sandbox_config,
        "setting_value",
        lambda name, default="": values.get(name, default),
    )

    with pytest.raises(ValueError, match="SANDBOX_CPUS"):
        sandbox_config.load_sandbox_config()


def test_sandbox_config_defaults_to_off(monkeypatch):
    import claw.sandbox.config as sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "setting_value",
        lambda _name, default="": default,
    )

    config = sandbox_config.load_sandbox_config()

    assert config.mode == "off"
    assert config.enabled is False
    assert config.project_venv is True
    assert config.stat_virtualization == "auto"
    assert config.pip_index_url == (
        "https://pypi.tuna.tsinghua.edu.cn/simple"
    )


def test_sandbox_config_rejects_invalid_project_venv_flag(monkeypatch):
    import claw.sandbox.config as sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "setting_value",
        lambda name, default="": (
            "sometimes" if name == "SANDBOX_PROJECT_VENV" else default
        ),
    )

    with pytest.raises(ValueError, match="SANDBOX_PROJECT_VENV"):
        sandbox_config.load_sandbox_config()


def test_sandbox_config_rejects_invalid_stat_virtualization(monkeypatch):
    import claw.sandbox.config as sandbox_config

    monkeypatch.setattr(
        sandbox_config,
        "setting_value",
        lambda name, default="": (
            "sometimes"
            if name == "SANDBOX_STAT_VIRTUALIZATION"
            else default
        ),
    )

    with pytest.raises(ValueError, match="SANDBOX_STAT_VIRTUALIZATION"):
        sandbox_config.load_sandbox_config()


def test_unbound_session_gets_private_persistent_workspace():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()

    manager.overwrite_file("s1", workspace, "notes/a.txt", "hello")
    assert manager.read_file("s1", workspace, "notes/a.txt") == (b"hello", False)
    assert backend.created[0]["host_workspace"] is None
    assert backend.created[0]["volume_name"].endswith("-workspace")

    manager.close_session("s1")
    manager.new_shell("s1", workspace)
    assert manager.read_file("s1", workspace, "notes/a.txt") == (b"hello", False)


def test_bound_workspace_change_recreates_microvm(tmp_path):
    backend = FakeBackend()
    manager = make_manager(backend)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    workspace = FakeWorkspaceManager(first)

    manager.new_shell("s1", workspace)
    workspace.workspace = second
    manager.new_shell("s1", workspace)

    assert len(backend.created) == 2
    assert backend.created[0]["host_workspace"] == first.resolve()
    assert backend.created[1]["host_workspace"] == second.resolve()
    assert len(backend.stopped) == 1


def test_stopped_microvm_is_recreated_with_the_same_private_volume():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.new_shell("s1", workspace)
    volume = backend.created[0]["volume_name"]

    backend.alive_value = False
    manager.new_shell("s1", workspace)

    assert len(backend.created) == 2
    assert backend.created[1]["volume_name"] == volume
    assert backend.stopped


def test_project_venv_is_bootstrapped_and_used_by_default_shell():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()

    shell = manager.new_shell("s1", workspace)
    result = manager.run_command("s1", workspace, "python -V", 10)

    bootstrap = next(
        command
        for command in backend.shell_commands
        if "SJTUCLAW_PROJECT_VENV_BOOTSTRAP" in command
    )
    command = backend.shell_commands[-1]
    assert "SJTUCLAW_PROJECT_VENV_BOOTSTRAP" in bootstrap
    assert "-m venv --without-pip --system-site-packages" in bootstrap
    assert "project_env_sync.py" in bootstrap
    assert "layout=sync-v1" in bootstrap
    assert shell["projectVenv"] == "/workspace/.venv"
    assert shell["python"] == "/opt/sjtuclaw/project-venv/bin/python"
    assert "export VIRTUAL_ENV=/opt/sjtuclaw/project-venv" in command
    assert "export SJTUCLAW_PROJECT_ENV=/workspace/.venv" in command
    assert "unset PIP_PREFIX PIP_TARGET PYTHONUSERBASE" in command
    assert 'export PATH="$VIRTUAL_ENV/bin:$PATH"' in command
    assert "project_env_sync.py" in command
    assert " save " in command
    assert "pypi.tuna.tsinghua.edu.cn/simple" in command
    assert result.ok


def test_project_venv_can_be_disabled_for_non_python_images():
    backend = FakeBackend()
    manager = SandboxManager(
        SandboxConfig(
            mode="required",
            image="alpine:3.21",
            project_venv=False,
        ),
        backend=backend,
    )
    workspace = FakeWorkspaceManager()

    shell = manager.new_shell("s1", workspace)
    manager.run_command("s1", workspace, "true", 10)

    assert shell["projectVenv"] is None
    assert shell["python"] == "image default"
    assert not any(
        "SJTUCLAW_PROJECT_VENV_BOOTSTRAP" in command
        for command in backend.shell_commands
    )
    assert "VIRTUAL_ENV" not in backend.shell_commands[-1]


def test_project_venv_bootstrap_failure_stops_new_microvm():
    class VenvFailBackend(FakeBackend):
        def shell(self, sandbox, command, *, cwd, timeout):
            if "SJTUCLAW_PROJECT_VENV_BOOTSTRAP" in command:
                self.shell_commands.append(command)
                return SimpleNamespace(
                    exit_code=72,
                    stdout_bytes=b"",
                    stderr_bytes=b"python missing",
                    output_limited=False,
                )
            return super().shell(
                sandbox,
                command,
                cwd=cwd,
                timeout=timeout,
            )

    backend = VenvFailBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()

    with pytest.raises(SandboxError, match="python missing"):
        manager.new_shell("s1", workspace)

    assert backend.stopped
    assert manager.status("s1", workspace)["running"] is False


@pytest.mark.parametrize(
    "path",
    ["../escape.txt", "/etc/passwd", r"C:\Windows\win.ini", r"\\server\share"],
)
def test_structured_paths_cannot_escape_guest_workspace(path):
    with pytest.raises(SandboxError):
        SandboxManager.guest_path(path)


def test_sandbox_path_errors_are_classified_as_workspace_boundaries():
    assert is_workspace_violation("路径超出 sandbox workspace: ../secret")
    assert is_workspace_violation("拒绝直接访问宿主绝对路径: C:\\secret")


def test_shell_can_change_to_microvm_path_outside_workspace():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()

    result = manager.run_command("s1", workspace, "cd /tmp\npwd", 10)

    assert result.ok
    assert result.cwd == "/tmp"
    assert result.stdout == "sandbox-output"


def test_required_mode_fails_closed_when_sdk_is_missing(monkeypatch):
    manager = SandboxManager(SandboxConfig(mode="required"))
    workspace = FakeWorkspaceManager()
    monkeypatch.setattr(manager, "sdk_available", lambda: False)

    with pytest.raises(SandboxError, match="拒绝回退到宿主执行"):
        manager.should_use("s1", workspace)


def test_auto_mode_preserves_legacy_path_when_runtime_probe_fails(monkeypatch):
    manager = SandboxManager(SandboxConfig(mode="auto"))
    monkeypatch.setattr(manager, "sdk_available", lambda: False)

    assert manager.should_use("s1", FakeWorkspaceManager()) is False


def test_session_sandbox_switches_are_isolated():
    backend = FakeBackend()
    manager = make_manager(backend, mode="auto")
    workspace = FakeWorkspaceManager()

    manager.new_shell("s1", workspace)
    manager.new_shell("s2", workspace)
    manager.set_session_enabled("s1", False, workspace)

    assert manager.is_session_enabled("s1") is False
    assert manager.is_session_enabled("s2") is True
    assert manager.should_use("s1", workspace) is False
    assert manager.should_use("s2", workspace) is True
    assert manager.status("s1", workspace)["running"] is False
    assert manager.status("s2", workspace)["running"] is True
    assert len(backend.stopped) == 1


def test_sandbox_on_overrides_global_off_for_only_one_session():
    manager = make_manager(FakeBackend(), mode="off")
    workspace = FakeWorkspaceManager()

    manager.set_session_enabled("s1", True, workspace)

    assert manager.should_use("s1", workspace) is True
    assert manager.should_use("s2", workspace) is False
    assert manager.status("s1", workspace)["effective"] is True
    assert manager.status("s2", workspace)["effective"] is False


def test_explicit_sandbox_state_survives_restart_and_fails_closed(
    tmp_path,
    monkeypatch,
):
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="s1")
    workspace = FakeWorkspaceManager()

    first = make_manager(FakeBackend(), mode="off")
    first.set_session_state_store(
        loader=lambda sid: store.get_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
        ),
        saver=lambda sid, enabled: store.set_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
            enabled,
        ),
    )
    first.set_session_enabled("s1", True, workspace)

    assert store.get_metadata_flag(
        "s1",
        SANDBOX_MODE_METADATA_KEY,
    ) is True

    restarted = SandboxManager(SandboxConfig(mode="off"))
    restarted.set_session_state_store(
        loader=lambda sid: store.get_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
        ),
        saver=lambda sid, enabled: store.set_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
            enabled,
        ),
    )
    monkeypatch.setattr(restarted, "sdk_available", lambda: False)

    assert restarted.is_session_enabled("s1") is True
    assert restarted.is_session_explicitly_enabled("s1") is True
    with pytest.raises(SandboxError, match="拒绝回退到宿主执行"):
        restarted.should_use("s1", workspace)


def test_explicit_sandbox_off_overrides_auto_mode_after_restart(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="s1")
    workspace = FakeWorkspaceManager()

    first = make_manager(FakeBackend(), mode="auto")
    first.set_session_state_store(
        loader=lambda sid: store.get_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
        ),
        saver=lambda sid, enabled: store.set_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
            enabled,
        ),
    )
    first.set_session_enabled("s1", False, workspace)

    restarted = make_manager(FakeBackend(), mode="auto")
    restarted.set_session_state_store(
        loader=lambda sid: store.get_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
        ),
        saver=lambda sid, enabled: store.set_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
            enabled,
        ),
    )

    assert restarted.is_session_enabled("s1") is False
    assert restarted.should_use("s1", workspace) is False


def test_purge_removes_explicit_sandbox_volume_after_off_mode_restart(
    monkeypatch,
):
    manager = SandboxManager(SandboxConfig(mode="off"))
    backend = FakeBackend()
    monkeypatch.setattr(manager, "sdk_available", lambda: True)
    monkeypatch.setattr(manager, "_backend_instance", lambda: backend)

    manager.purge_session("s1")

    assert len(backend.removed_volumes) == 1


def test_gateway_sandbox_badge_state_is_session_scoped(monkeypatch):
    from claw.gateway import server

    manager = make_manager(FakeBackend(), mode="auto")
    workspace = FakeWorkspaceManager()
    monkeypatch.setattr(server, "_sandbox_manager", manager)
    monkeypatch.setattr(server, "_workspace_manager", workspace)

    manager.set_session_enabled("s1", False, workspace)

    assert server._session_sandbox_mode("s1") is False
    assert server._session_sandbox_mode("s2") is True


def test_gateway_passes_effective_sandbox_to_agent_turn(monkeypatch, tmp_path):
    import asyncio

    from claw.gateway import server
    from claw.gateway.server import ChatRequest

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="sandbox-auto")
    store.save(session)
    captured = {}

    def fake_run_agent_turn(session_id, message, **kwargs):
        captured.update(kwargs)
        current = store.get(session_id)
        current.append_message("user", message)
        current.append_message("assistant", "done")
        store.save(current)
        return "done"

    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_workspace_manager", FakeWorkspaceManager())
    monkeypatch.setattr(server, "_auto_mode", {session.session_id: True})
    monkeypatch.setattr(server, "_llm_ready", lambda _sid: True)
    monkeypatch.setattr(server, "_session_sandbox_mode", lambda _sid: True)
    monkeypatch.setattr(server, "_session_backend", lambda _sid: "sjtuclaw")
    monkeypatch.setattr(server, "_session_pi_mode", lambda _sid: False)
    monkeypatch.setattr(server, "list_downloads", lambda: [])
    monkeypatch.setattr(server, "auto_title_if_first_turn", lambda *args: None)
    monkeypatch.setattr(server, "run_agent_turn", fake_run_agent_turn)

    response = asyncio.run(
        server.handle_chat(
            ChatRequest(sessionId=session.session_id, message="run tests")
        )
    )

    assert response["reply"] == "done"
    assert captured["auto_mode"] is True
    assert captured["sandbox_enabled"] is True


def test_gateway_new_session_defaults_sandbox_off(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    manager = SandboxManager(SandboxConfig(), backend=FakeBackend())
    workspace = FakeWorkspaceManager()
    monkeypatch.setattr(server, "_sandbox_manager", manager)
    monkeypatch.setattr(server, "_workspace_manager", workspace)
    monkeypatch.setattr(
        server,
        "_session_store",
        SessionStore(tmp_path / "sessions"),
    )

    response = server.create_session()

    assert response["sandboxMode"] is False
    assert manager.is_session_enabled(response["sessionId"]) is False


def test_gateway_restores_persisted_auto_modes(tmp_path):
    from claw.gateway import server

    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="enabled")
    store.create_session(session_id="disabled")
    store.set_metadata_flag("enabled", AUTO_MODE_METADATA_KEY, True)
    store.set_metadata_flag("disabled", AUTO_MODE_METADATA_KEY, False)

    assert server._load_persisted_auto_modes(store) == {"enabled": True}


def test_explicit_sandbox_blocks_switch_to_external_backend(
    monkeypatch,
    tmp_path,
):
    from claw.gateway import server
    from claw.pi import get_session_backend
    import claw.pi as pi_module

    store = SessionStore(tmp_path / "sessions")
    store.create_session(session_id="s1")
    manager = make_manager(FakeBackend(), mode="off")
    manager.set_session_state_store(
        loader=lambda sid: store.get_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
        ),
        saver=lambda sid, enabled: store.set_metadata_flag(
            sid,
            SANDBOX_MODE_METADATA_KEY,
            enabled,
        ),
    )
    manager.set_agent_backend_provider(
        lambda sid: get_session_backend(store, sid)
    )
    manager.set_session_enabled("s1", True, FakeWorkspaceManager())

    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_sandbox_manager", manager)
    monkeypatch.setattr(server, "_session_turn_active", lambda _sid: False)
    monkeypatch.setattr(pi_module, "load_pi_config", lambda: object())

    result = server._execute_slash_command("/pi on", "s1")

    assert "请先使用 /sandbox off" in result
    assert get_session_backend(store, "s1") == "sjtuclaw"


def test_cli_sandbox_on_and_off_change_only_current_session():
    from claw.cli.commands import _handle_sandbox_command

    manager = make_manager(FakeBackend(), mode="auto")
    workspace = FakeWorkspaceManager()
    state = SimpleNamespace(
        current_session_id="s1",
        sandbox_manager=manager,
        workspace_manager=workspace,
    )

    assert "已关闭" in _handle_sandbox_command(["off"], state)
    state.current_session_id = "s2"
    assert "已开启" in _handle_sandbox_command(["status"], state)
    state.current_session_id = "s1"
    assert "已关闭" in _handle_sandbox_command(["status"], state)
    assert "已开启" in _handle_sandbox_command(["on"], state)


def test_cli_sandbox_status_does_not_claim_on_demand_start_when_ineffective():
    from claw.cli.commands import _handle_sandbox_command

    manager = make_manager(FakeBackend(), mode="auto")
    workspace = FakeWorkspaceManager(unlimited=True)
    state = SimpleNamespace(
        current_session_id="s1",
        sandbox_manager=manager,
        workspace_manager=workspace,
    )

    result = _handle_sandbox_command(["status"], state)

    assert "当前未生效" in result
    assert "未运行" in result
    assert "按需启动" not in result


def test_cli_passes_effective_sandbox_to_agent_turn(monkeypatch, tmp_path):
    from claw.cli import repl
    from claw.cli.commands import RuntimeState

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="cli-sandbox-auto")
    store.save(session)
    manager = make_manager(FakeBackend(), mode="auto")
    workspace = FakeWorkspaceManager()
    captured = {}

    def fake_run_agent_turn(_session_id, _message, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(repl, "run_agent_turn", fake_run_agent_turn)
    monkeypatch.setattr(repl, "_maybe_auto_title", lambda *_args: None)
    state = RuntimeState(
        session_store=store,
        memory_store=SimpleNamespace(),
        llm_client=SimpleNamespace(),
        current_session_id=session.session_id,
        workspace_manager=workspace,
        sandbox_manager=manager,
        auto_mode=True,
    )

    repl._handle_chat_turn(
        "run tests",
        state,
        state.llm_client,
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert captured["auto_mode"] is True
    assert captured["sandbox_enabled"] is True


def test_required_mode_rejects_session_sandbox_off():
    manager = make_manager(FakeBackend(), mode="required")

    with pytest.raises(SandboxError, match="不允许关闭"):
        manager.set_session_enabled(
            "s1",
            False,
            FakeWorkspaceManager(),
        )


def test_required_mode_rejects_unlimited_even_with_backend():
    manager = make_manager(FakeBackend())
    workspace = FakeWorkspaceManager(unlimited=True)

    with pytest.raises(SandboxError, match="UNLIMITED"):
        manager.should_use("s1", workspace)


def test_auto_mode_does_not_route_external_agent_host_tools_to_microvm():
    manager = make_manager(FakeBackend(), mode="auto")
    manager.set_agent_backend_provider(lambda _sid: "pi")

    assert manager.should_use("s1", FakeWorkspaceManager()) is False
    assert manager.status("s1", FakeWorkspaceManager())["covered"] is False


def test_required_mode_rejects_persisted_external_agent_session():
    manager = make_manager(FakeBackend())
    manager.set_agent_backend_provider(lambda _sid: "claude")

    with pytest.raises(SandboxError, match="尚未纳入 microVM"):
        manager.should_use("s1", FakeWorkspaceManager())


def test_native_file_tools_share_sandbox_without_host_workspace():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    registry = ToolRegistry()
    register_all_tools(
        registry,
        workspace_manager=workspace,
        sandbox_manager=manager,
        session_id_provider=lambda: "s1",
    )

    write_result = registry.get_tool("overwrite_file").handler(
        {"path": "result.txt", "content": "from sandbox"}
    )
    read_result = registry.get_tool("read_file").handler({"path": "result.txt"})
    shell_result = registry.get_tool("run_command").handler(
        {"command": "pwd", "timeout": 10}
    )

    assert write_result.ok
    assert read_result.content == "from sandbox"
    assert shell_result.ok
    assert '"execution": "microsandbox"' in shell_result.content


def test_shell_tool_reports_hard_output_limit_and_bounds_response():
    class LimitedBackend(FakeBackend):
        def shell(self, sandbox, command, *, cwd, timeout):
            result = super().shell(
                sandbox,
                command,
                cwd=cwd,
                timeout=timeout,
            )
            if "SJTUCLAW_PROJECT_VENV_BOOTSTRAP" in command:
                return result
            marker_tail = result.stdout_tail_bytes
            result.stdout_bytes = b"x" * (70 * 1024)
            result.stdout_tail_bytes = marker_tail
            result.stdout_truncated = True
            result.output_limited = True
            return result

    manager = make_manager(LimitedBackend())
    registry = ToolRegistry()
    register_all_tools(
        registry,
        workspace_manager=FakeWorkspaceManager(),
        sandbox_manager=manager,
        session_id_provider=lambda: "s1",
    )

    tool_result = registry.get_tool("run_command").handler(
        {"command": "yes", "timeout": 10}
    )
    payload = json.loads(tool_result.error)

    assert not tool_result.ok
    assert payload["output_limited"] is True
    assert payload["stdout_truncated"] is True
    assert "8 MiB" in payload["error"]
    assert len(payload["stdout"].encode("utf-8")) <= 64 * 1024


def test_attachment_import_and_download_export_bridge_the_microvm(
    tmp_path, monkeypatch
):
    import claw.sandbox.runtime as runtime
    from claw.tools import download

    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    sessions_dir = tmp_path / "sessions"
    attachment_dir = sessions_dir / "s1" / "attachments"
    attachment_dir.mkdir(parents=True)
    (attachment_dir / "stored.bin").write_bytes(b"attachment")
    (attachment_dir / ".meta.json").write_text(
        json.dumps(
            [
                {
                    "id": "att-1",
                    "storedName": "stored.bin",
                    "originalName": "input.bin",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    monkeypatch.setattr(download, "DATA_DIR", tmp_path)
    download.configure_download_registry(None)
    registry = ToolRegistry()
    register_all_tools(
        registry,
        workspace_manager=workspace,
        sandbox_manager=manager,
        session_id_provider=lambda: "s1",
        sessions_dir=sessions_dir,
    )

    copied = registry.get_tool("copy_attachment_to_workspace").handler(
        {"attachment_id": "att-1", "dest_path": "inputs/input.bin"}
    )
    exported = registry.get_tool("create_download").handler(
        {"path": "inputs/input.bin"}
    )

    assert copied.ok
    assert exported.ok
    payload = json.loads(exported.content)
    host_path = download.get_download(payload["downloadId"])
    assert host_path is not None
    assert host_path.read_bytes() == b"attachment"
    download.configure_download_registry(None)


def test_export_copies_sandbox_file_to_managed_host_path(tmp_path, monkeypatch):
    import claw.sandbox.runtime as runtime

    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    monkeypatch.setattr(runtime, "DATA_DIR", tmp_path)
    manager.overwrite_file("s1", workspace, "report.txt", "report")

    exported = manager.export_file("s1", workspace, "report.txt")

    assert exported.is_file()
    assert exported.read_text(encoding="utf-8") == "report"
    assert exported.is_relative_to(tmp_path / "sandbox" / "exports")


def test_export_name_is_safe_for_windows():
    assert SandboxManager._safe_export_name('report:final?.txt') == (
        "report_final_.txt"
    )
    assert SandboxManager._safe_export_name("CON.txt") == "_CON.txt"


def test_purge_removes_private_volume():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.new_shell("s1", workspace)
    volume = backend.created[0]["volume_name"]

    manager.purge_session("s1")

    assert volume in backend.removed_volumes
    assert backend.stopped


def test_bounded_capture_retains_only_configured_head_and_tail():
    capture = _BoundedCapture(head_limit=4, tail_limit=3)

    capture.append(b"abc")
    capture.append(b"defgh")

    assert capture.seen == 8
    assert bytes(capture.head) == b"abcd"
    assert bytes(capture.tail) == b"fgh"
    assert capture.truncated


def test_manager_decodes_non_utf8_shell_output_with_replacement():
    class BinaryBackend(FakeBackend):
        def shell(self, sandbox, command, *, cwd, timeout):
            result = super().shell(
                sandbox,
                command,
                cwd=cwd,
                timeout=timeout,
            )
            result.stdout_bytes = b"\xff\n" + result.stdout_bytes
            result.stdout_tail_bytes = result.stdout_bytes[-4096:]
            return result

    manager = make_manager(BinaryBackend())
    result = manager.run_command(
        "s1",
        FakeWorkspaceManager(),
        "printf '\\377'",
        10,
    )

    assert result.ok
    assert result.stdout == "\ufffd\nsandbox-output"


def test_manager_does_not_misclassify_unrelated_timeout_text():
    class InvalidTimeoutBackend(FakeBackend):
        def shell(self, *_args, **_kwargs):
            raise OSError("invalid timeout configuration")

    manager = make_manager(InvalidTimeoutBackend())

    with pytest.raises(SandboxError, match="invalid timeout configuration"):
        manager.run_command("s1", FakeWorkspaceManager(), "true", 10)


def test_read_file_passes_limit_to_backend_before_loading_payload():
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.overwrite_file("s1", workspace, "large.bin", "x" * 1024)

    payload, truncated = manager.read_file(
        "s1",
        workspace,
        "large.bin",
        max_bytes=32,
    )

    assert payload == b"x" * 32
    assert truncated
    assert backend.read_limits[-1] == 32


def test_overwrite_failure_preserves_original_and_cleans_temporary_file():
    class PartialWriteBackend(FakeBackend):
        fail_writes = False

        def write(self, sandbox, path, data):
            super().write(sandbox, path, data[:3])
            if self.fail_writes and ".sjtuclaw-" in path:
                raise OSError("simulated partial write")
            super().write(sandbox, path, data)

    backend = PartialWriteBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.overwrite_file("s1", workspace, "result.txt", "original")
    backend.fail_writes = True

    with pytest.raises(OSError, match="partial write"):
        manager.overwrite_file("s1", workspace, "result.txt", "replacement")

    assert manager.read_file("s1", workspace, "result.txt") == (
        b"original",
        False,
    )
    files = next(iter(backend._stores.values()))
    assert not any(".sjtuclaw-" in path for path in files)


def test_import_uses_temporary_file_then_atomic_rename(tmp_path):
    backend = FakeBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    source = tmp_path / "attachment.bin"
    source.write_bytes(b"new attachment")
    manager.overwrite_file("s1", workspace, "input.bin", "old")

    manager.import_file("s1", workspace, source, "input.bin")

    assert manager.read_file("s1", workspace, "input.bin") == (
        b"new attachment",
        False,
    )
    files = next(iter(backend._stores.values()))
    assert not any(".sjtuclaw-" in path for path in files)


def test_close_failure_is_reported_and_session_is_retained_for_retry():
    class StopFailBackend(FakeBackend):
        def stop(self, _sandbox, _name):
            raise OSError("cannot stop")

    backend = StopFailBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.new_shell("s1", workspace)

    with pytest.raises(SandboxError, match="cannot stop"):
        manager.close_session("s1")

    assert manager.status("s1", workspace)["running"] is True


def test_cli_sandbox_off_does_not_report_success_when_stop_fails():
    from claw.cli.commands import _handle_sandbox_command

    class StopFailBackend(FakeBackend):
        def stop(self, _sandbox, _name):
            raise OSError("cannot stop")

    manager = make_manager(StopFailBackend(), mode="auto")
    workspace = FakeWorkspaceManager()
    manager.new_shell("s1", workspace)
    state = SimpleNamespace(
        current_session_id="s1",
        sandbox_manager=manager,
        workspace_manager=workspace,
    )

    message = _handle_sandbox_command(["off"], state)

    assert message.startswith("[错误]")
    assert "cannot stop" in message
    assert manager.status("s1", workspace)["running"] is True


def test_workspace_change_does_not_replace_vm_when_old_vm_cannot_stop(tmp_path):
    class StopFailBackend(FakeBackend):
        def stop(self, _sandbox, _name):
            raise OSError("cannot stop")

    backend = StopFailBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager(tmp_path)
    manager.new_shell("s1", workspace)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    workspace.workspace = replacement

    with pytest.raises(SandboxError, match="未创建替代环境"):
        manager.new_shell("s1", workspace)

    assert len(backend.created) == 1
    assert manager.status("s1", workspace)["running"] is True


def test_purge_reports_volume_cleanup_failure():
    class RemoveFailBackend(FakeBackend):
        def remove_volume(self, volume_name):
            self.removed_volumes.append(volume_name)
            raise OSError("volume busy")

    backend = RemoveFailBackend()
    manager = make_manager(backend)
    workspace = FakeWorkspaceManager()
    manager.new_shell("s1", workspace)

    with pytest.raises(SandboxError, match="volume busy"):
        manager.purge_session("s1")

    assert manager.status("s1", workspace)["running"] is False


def test_close_all_logs_cleanup_failures(caplog):
    class CleanupFailBackend(FakeBackend):
        def stop(self, _sandbox, _name):
            raise OSError("cannot stop")

        def close(self):
            raise OSError("cannot close")

    backend = CleanupFailBackend()
    manager = make_manager(backend)
    manager.new_shell("s1", FakeWorkspaceManager())

    manager.close_all()

    assert "应用退出时停止 sandbox 失败" in caplog.text
    assert "应用退出时关闭 microsandbox 后端失败" in caplog.text


def test_concrete_backend_streams_with_hard_output_limit(monkeypatch):
    import claw.sandbox.runtime as runtime

    class FakeHandle:
        def __init__(self):
            self.events = [
                SimpleNamespace(event_type="stdout", data=b"abcdefgh", code=None),
                SimpleNamespace(event_type="stdout", data=b"ijklmnop", code=None),
                SimpleNamespace(event_type="exited", data=None, code=137),
            ]
            self.killed = False

        async def recv(self):
            return self.events.pop(0) if self.events else None

        async def kill(self):
            self.killed = True

    class FakeSandbox:
        def __init__(self):
            self.handle = FakeHandle()

        async def shell_stream(self, *_args, **_kwargs):
            return self.handle

    monkeypatch.setattr(runtime, "_MAX_STREAM_BYTES", 10)
    monkeypatch.setattr(runtime, "_STREAM_CAPTURE_BYTES", 6)
    backend = MicrosandboxBackend.__new__(MicrosandboxBackend)
    backend._bridge = _AsyncBridge()
    sandbox = FakeSandbox()
    try:
        output = backend.shell(
            sandbox,
            "yes",
            cwd=GUEST_WORKSPACE,
            timeout=10,
        )
    finally:
        backend.close()

    assert sandbox.handle.killed
    assert output.output_limited
    assert output.stdout_truncated
    assert output.stdout_bytes == b"abcdef"


def test_concrete_backend_translates_sdk_timeout_and_kills_handle():
    class FakeSdkTimeout(Exception):
        pass

    class FakeHandle:
        killed = False

        async def recv(self):
            raise FakeSdkTimeout("execution timed out")

        async def kill(self):
            self.killed = True

    class FakeSandbox:
        def __init__(self):
            self.handle = FakeHandle()

        async def shell_stream(self, *_args, **_kwargs):
            return self.handle

    backend = MicrosandboxBackend.__new__(MicrosandboxBackend)
    backend._msb = SimpleNamespace(ExecTimeoutError=FakeSdkTimeout)
    backend._bridge = _AsyncBridge()
    sandbox = FakeSandbox()
    try:
        with pytest.raises(TimeoutError):
            backend.shell(
                sandbox,
                "sleep 60",
                cwd=GUEST_WORKSPACE,
                timeout=10,
            )
    finally:
        backend.close()

    assert sandbox.handle.killed


def test_concrete_backend_read_stream_stops_after_limit():
    class FakeFs:
        async def read_stream(self, _path):
            async def chunks():
                yield b"abcd"
                yield b"efgh"
                yield b"unused"

            return chunks()

    backend = MicrosandboxBackend.__new__(MicrosandboxBackend)
    backend._bridge = _AsyncBridge()
    try:
        payload, truncated = backend.read_limited(
            SimpleNamespace(fs=FakeFs()),
            "/workspace/file.bin",
            5,
        )
    finally:
        backend.close()

    assert payload == b"abcde"
    assert truncated
