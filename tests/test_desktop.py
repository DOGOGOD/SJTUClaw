from __future__ import annotations

import sys
import threading
import time

import pytest

from claw import desktop


class _StoppedThread:
    def is_alive(self) -> bool:
        return False


class _FakeProcessFailure:
    ProcessFailedKind = "RenderProcessExited"
    Reason = "Crashed"
    ExitCode = -1


class _FakeWebViewSender:
    def __init__(self):
        self.reloads = 0

    def Reload(self):
        self.reloads += 1


def test_wait_until_ready_fails_fast_when_server_thread_exits():
    with pytest.raises(RuntimeError, match="意外退出"):
        desktop._wait_until_ready(
            "http://127.0.0.1:65535",
            timeout_s=20,
            server_thread=_StoppedThread(),
        )


def test_wait_until_ready_raises_after_timeout(monkeypatch):
    clock = iter((0.0, 0.1, 0.2))
    monkeypatch.setattr(desktop.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(desktop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )

    with pytest.raises(TimeoutError, match="未能启动"):
        desktop._wait_until_ready("http://127.0.0.1:65535", timeout_s=0.15)


def test_webview_renderer_crash_reloads_automatically(monkeypatch):
    sender = _FakeWebViewSender()
    logs: list[str] = []
    monkeypatch.setattr(desktop, "_log", logs.append)
    monkeypatch.setattr(desktop, "_webview_recovery_attempts", [])

    desktop._handle_webview_process_failed(sender, _FakeProcessFailure())

    assert sender.reloads == 1
    assert any("RenderProcessExited" in message for message in logs)
    assert any("reloaded automatically" in message for message in logs)


def test_webview_gpu_failure_does_not_reload(monkeypatch):
    sender = _FakeWebViewSender()
    failure = _FakeProcessFailure()
    failure.ProcessFailedKind = "GpuProcessExited"
    monkeypatch.setattr(desktop, "_log", lambda _message: None)
    monkeypatch.setattr(desktop, "_webview_recovery_attempts", [])

    desktop._handle_webview_process_failed(sender, failure)

    assert sender.reloads == 0


def test_webview_recovery_stops_after_repeated_renderer_failures(monkeypatch):
    sender = _FakeWebViewSender()
    logs: list[str] = []
    monkeypatch.setattr(desktop, "_log", logs.append)
    monkeypatch.setattr(desktop, "_webview_recovery_attempts", [])
    monkeypatch.setattr(desktop.time, "monotonic", lambda: 10.0)

    for _ in range(desktop._WEBVIEW_RECOVERY_LIMIT + 1):
        desktop._handle_webview_process_failed(sender, _FakeProcessFailure())

    assert sender.reloads == desktop._WEBVIEW_RECOVERY_LIMIT
    assert any("skipped after repeated renderer failures" in message for message in logs)


def test_main_does_not_open_window_when_gateway_is_unavailable(
    monkeypatch,
    tmp_path,
):
    shown: list[str] = []
    opened: list[str] = []

    monkeypatch.setattr(sys, "argv", ["sjtuclaw", "desktop"])
    monkeypatch.setattr(desktop, "user_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_choose_port", lambda: 18765)
    monkeypatch.setattr(desktop, "_run_server", lambda *_args: None)
    monkeypatch.setattr(
        desktop,
        "_wait_until_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("gateway failed")
        ),
    )
    monkeypatch.setattr(desktop, "_show_startup_error", shown.append)
    monkeypatch.setattr(desktop, "_run_window", opened.append)

    assert desktop.main() == 1
    assert shown and "gateway failed" in shown[0]
    assert opened == []


def test_main_stops_gateway_after_window_closes(monkeypatch, tmp_path):
    class _FakeServer:
        should_exit = False

    server = _FakeServer()
    started = threading.Event()
    stopped = threading.Event()
    opened: list[str] = []

    def run_server(fake_server):
        started.set()
        while not fake_server.should_exit:
            time.sleep(0.001)
        stopped.set()

    def wait_until_ready(*_args, **_kwargs):
        assert started.wait(timeout=1)

    monkeypatch.setattr(sys, "argv", ["sjtuclaw", "desktop"])
    monkeypatch.setattr(desktop, "user_root", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_choose_port", lambda: 18765)
    monkeypatch.setattr(desktop, "_create_server", lambda *_args: server)
    monkeypatch.setattr(desktop, "_run_server", run_server)
    monkeypatch.setattr(desktop, "_wait_until_ready", wait_until_ready)
    monkeypatch.setattr(desktop, "_run_window", opened.append)

    assert desktop.main() == 0
    assert opened == ["http://127.0.0.1:18765"]
    assert server.should_exit is True
    assert stopped.is_set()
