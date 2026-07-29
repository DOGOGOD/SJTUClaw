"""Desktop launcher for the packaged SJTUClaw app."""

from __future__ import annotations

import argparse
import ctypes
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import traceback
from pathlib import Path

import uvicorn

from claw.env_utils import env_int
from claw.paths import resource_root, user_root
from claw.utils import force_utf8_stdio


_WEBVIEW_RECOVERY_LIMIT = 2
_WEBVIEW_RECOVERY_WINDOW_SECONDS = 60.0
_webview_recovery_attempts: list[float] = []
_webview_process_failed_handlers: list[object] = []


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def _choose_port() -> int:
    requested = env_int("GATEWAY_PORT", 8000, minimum=1, maximum=65535)
    if _port_available(requested):
        return requested
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(
    url: str,
    timeout_s: float = 20.0,
    *,
    server_thread: threading.Thread | None = None,
) -> None:
    """Wait for the local Gateway or fail before opening the desktop window."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if server_thread is not None and not server_thread.is_alive():
            raise RuntimeError("本地 Gateway 启动进程已意外退出。")
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise TimeoutError(f"本地 Gateway 在 {timeout_s:g} 秒内未能启动。")


def _log_path() -> Path:
    path = user_root() / "logs" / "desktop.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log(message: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(message.rstrip() + "\n")
    except OSError:
        pass


def _show_startup_error(message: str) -> None:
    """Show a visible error for the windowless Windows desktop executable."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            message,
            "SJTUClaw 启动失败",
            0x10,
        )
    except (AttributeError, OSError):
        pass


def _create_server(host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        "claw.gateway.server:app",
        host=host,
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )
    return uvicorn.Server(config)


def _run_server(server: uvicorn.Server) -> None:
    try:
        server.run()
    except Exception:
        _log(traceback.format_exc())
        raise


def _stop_server(
    server: uvicorn.Server,
    thread: threading.Thread,
    *,
    timeout_s: float = 7.0,
) -> None:
    """Stop the Gateway so its lifespan cleanup also closes child processes."""
    server.should_exit = True
    if thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout_s)
    if thread.is_alive():
        _log(f"Gateway did not stop within {timeout_s:g} seconds.")


def _window_icon_path() -> str | None:
    candidates = [
        Path(sys.executable).resolve().parent / "SJTUClaw.ico",
        resource_root() / "packaging" / "windows" / "assets" / "SJTUClaw.ico",
        resource_root() / "web" / "favicon.ico",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _webview_failure_value(args: object, name: str, default: object = "unknown") -> object:
    try:
        return getattr(args, name)
    except (AttributeError, RuntimeError):
        getter = getattr(args, f"get_{name}", None)
        if callable(getter):
            try:
                return getter()
            except (AttributeError, RuntimeError):
                pass
    return default


def _webview_failure_text(args: object, name: str) -> str:
    value = _webview_failure_value(args, name)
    to_string = getattr(value, "ToString", None)
    if callable(to_string):
        try:
            return str(to_string())
        except (AttributeError, RuntimeError):
            pass
    return str(value)


def _handle_webview_process_failed(sender: object, args: object) -> None:
    """Log WebView2 failures and recover a crashed main-frame renderer."""
    global _webview_recovery_attempts

    kind = _webview_failure_text(args, "ProcessFailedKind")
    reason = _webview_failure_text(args, "Reason")
    exit_code = _webview_failure_value(args, "ExitCode")
    _log(
        "WebView2 process failed: "
        f"kind={kind}, reason={reason}, exitCode={exit_code}"
    )

    if kind != "RenderProcessExited":
        return

    now = time.monotonic()
    _webview_recovery_attempts = [
        attempt
        for attempt in _webview_recovery_attempts
        if now - attempt < _WEBVIEW_RECOVERY_WINDOW_SECONDS
    ]
    if len(_webview_recovery_attempts) >= _WEBVIEW_RECOVERY_LIMIT:
        _log("WebView2 automatic reload skipped after repeated renderer failures.")
        return

    _webview_recovery_attempts.append(now)
    try:
        sender.Reload()
        _log("WebView2 renderer reloaded automatically.")
    except Exception:
        _log("WebView2 automatic reload failed:\n" + traceback.format_exc())


def _install_webview_recovery(window: object) -> None:
    """Attach WebView2 process recovery after pywebview initializes."""
    if os.name != "nt":
        return

    events = getattr(window, "events", None)
    loaded = getattr(events, "loaded", None)
    if loaded is None or not loaded.wait(20):
        _log("WebView2 recovery handler was not installed before startup timeout.")
        return

    try:
        native = getattr(window, "native")
        control = getattr(native, "webview")
        core = getattr(control, "CoreWebView2")

        def process_failed(sender, args):
            _handle_webview_process_failed(sender, args)

        def attach():
            core.add_ProcessFailed(process_failed)

        if getattr(native, "InvokeRequired", False):
            from System import Action

            native.Invoke(Action(attach))
        else:
            attach()
        _webview_process_failed_handlers.append(process_failed)
        _log("WebView2 process recovery handler installed.")
    except Exception:
        _log("WebView2 recovery handler installation failed:\n" + traceback.format_exc())


def _run_window(url: str) -> None:
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    window = webview.create_window(
        "SJTUClaw",
        url,
        width=1280,
        height=820,
        min_size=(960, 640),
        text_select=True,
    )
    webview.start(
        _install_webview_recovery,
        args=(window,),
        gui="edgechromium" if os.name == "nt" else None,
        icon=_window_icon_path(),
    )


def _run_sandbox_self_test(report_path: Path, workspace: Path) -> int:
    """Run one real frozen sandbox tool command and persist a QA report."""
    import json
    import uuid
    from dataclasses import replace

    from claw.sandbox import SandboxManager, load_sandbox_config
    from claw.utils import atomic_write

    class _Workspace:
        def get(self, _session_id: str) -> Path:
            return workspace

        def is_unlimited(self, _session_id: str) -> bool:
            return False

    workspace.mkdir(parents=True, exist_ok=True)
    session_id = f"desktop-self-test-{uuid.uuid4().hex}"
    manager: SandboxManager | None = None
    report: dict[str, object] = {"ok": False}
    exit_code = 1
    try:
        config = replace(load_sandbox_config(), mode="required")
        manager = SandboxManager(config)
        manager.set_agent_backend_provider(lambda _sid: "sjtuclaw")
        shell = manager.new_shell(session_id, _Workspace())
        result = manager.run_command(
            session_id,
            _Workspace(),
            "printf 'frozen-tool-ok\\n'",
            60,
        )
        if not result.ok or "frozen-tool-ok" not in result.stdout:
            raise RuntimeError(result.stderr or f"命令退出码 {result.exit_code}")
        import microsandbox._microsandbox as native_msb

        report = {
            "ok": True,
            "runtime": native_msb.resolved_msb_path(),
            "shell": shell,
            "stdout": result.stdout,
        }
        exit_code = 0
    except BaseException as exc:
        report = {
            "ok": False,
            "errorType": type(exc).__name__,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        if manager is not None:
            try:
                manager.purge_session(session_id)
                manager.close_all()
            except Exception:
                report["cleanupError"] = traceback.format_exc()
                exit_code = 1
        atomic_write(
            report_path,
            json.dumps(report, ensure_ascii=False, indent=2),
        )
    return exit_code


def main() -> int:
    force_utf8_stdio()
    # The packaged app's default agent directory must exist before the first
    # tool call.  Source runs keep using the checkout root and need no setup.
    user_root().mkdir(parents=True, exist_ok=True)
    parser = argparse.ArgumentParser(description="SJTUClaw desktop launcher")
    parser.add_argument("--pet", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--server-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--sandbox-self-test-report",
        type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--sandbox-self-test-workspace",
        type=Path,
        help=argparse.SUPPRESS,
    )
    args, _ = parser.parse_known_args()
    if args.pet:
        # Keep the Tk-based pet isolated from the main desktop launcher.
        # This also lets the launcher report packaging problems without
        # crashing before the Gateway or webview window can start.
        from claw.pet.__main__ import main as pet_main

        sys.argv = [sys.argv[0], *(arg for arg in sys.argv[1:] if arg != "--pet")]
        return pet_main()
    if args.sandbox_self_test_report:
        workspace = (
            args.sandbox_self_test_workspace
            or user_root() / "data" / "sandbox-self-test"
        )
        return _run_sandbox_self_test(
            args.sandbox_self_test_report.resolve(),
            workspace.resolve(),
        )

    host = "127.0.0.1"
    port = _choose_port()
    os.environ["GATEWAY_HOST"] = host
    os.environ["GATEWAY_PORT"] = str(port)
    url = f"http://{host}:{port}"
    _log(f"Starting SJTUClaw desktop gateway at {url}")
    server = _create_server(host, port)

    if args.server_only:
        _run_server(server)
        return 0

    thread = threading.Thread(target=_run_server, args=(server,), daemon=True)
    thread.start()
    try:
        _wait_until_ready(url, server_thread=thread)
    except (RuntimeError, TimeoutError) as exc:
        message = f"{exc}\n请查看日志：{_log_path()}"
        _log(message)
        _show_startup_error(message)
        _stop_server(server, thread)
        return 1
    try:
        _run_window(url)
    finally:
        _stop_server(server, thread)
    return 0


if __name__ == "__main__":
    sys.exit(main())
