from __future__ import annotations

from claw.cli.repl import _wait_for_compaction_on_exit


class _Worker:
    def __init__(self, *, running: bool, wait_result: bool = True):
        self.running = running
        self.wait_result = wait_result
        self.wait_calls: list[float] = []

    def is_running(self) -> bool:
        return self.running

    def wait(self, timeout: float) -> bool:
        self.wait_calls.append(timeout)
        return self.wait_result


def test_cli_exit_is_silent_when_compaction_is_idle(capsys):
    worker = _Worker(running=False)

    _wait_for_compaction_on_exit(worker)

    assert worker.wait_calls == []
    assert capsys.readouterr().out == ""


def test_cli_exit_waits_when_compaction_is_running(capsys):
    worker = _Worker(running=True)

    _wait_for_compaction_on_exit(worker)

    assert worker.wait_calls == [5.0]
    output = capsys.readouterr().out
    assert "等待后台压缩任务完成" in output
    assert "警告" not in output


def test_cli_exit_warns_when_running_compaction_times_out(capsys):
    worker = _Worker(running=True, wait_result=False)

    _wait_for_compaction_on_exit(worker)

    assert worker.wait_calls == [5.0]
    output = capsys.readouterr().out
    assert "等待后台压缩任务完成" in output
    assert "未在 5 秒内完成" in output
