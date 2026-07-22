from pathlib import Path

from claw.tools import readonly


def test_readonly_relative_path_uses_main_dir_without_workspace(monkeypatch, tmp_path):
    main = tmp_path / "project-root"
    launch = tmp_path / "webui"
    main.mkdir()
    launch.mkdir()
    (main / "root-marker.txt").write_text("root", encoding="utf-8")
    (launch / "frontend-marker.txt").write_text("frontend", encoding="utf-8")
    monkeypatch.setattr(readonly, "main_dir", lambda: main)
    monkeypatch.chdir(launch)

    target = readonly._resolve_path(".", None, None)
    result = readonly._make_list_dir_handler()(dict(path="."))

    assert target == main.resolve()
    assert result.ok
    assert "root-marker.txt" in result.content
    assert "frontend-marker.txt" not in result.content


def test_readonly_relative_path_uses_main_dir_for_unbound_session(monkeypatch, tmp_path):
    main = tmp_path / "project-root"
    main.mkdir()
    monkeypatch.setattr(readonly, "main_dir", lambda: main)

    class UnboundWorkspaceManager:
        def is_unlimited(self, _session_id: str) -> bool:
            return False

        def get(self, _session_id: str) -> Path | None:
            return None

    target = readonly._resolve_path(
        ".", UnboundWorkspaceManager(), lambda: "session_001"
    )

    assert target == main.resolve()
