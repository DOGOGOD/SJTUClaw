from pathlib import Path

from claw import paths


def test_user_root_defaults_to_hidden_directory_in_current_home(monkeypatch):
    monkeypatch.delenv("SJTUCLAW_USER_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("C:/Users/tester")))

    assert paths.user_root() == Path("C:/Users/tester/.sjtuclaw")


def test_user_root_override_is_preserved(monkeypatch, tmp_path):
    override = tmp_path / "custom-user-data"
    monkeypatch.setenv("SJTUCLAW_USER_DIR", str(override))

    assert paths.user_root() == override.resolve()


def test_frozen_data_dir_is_below_per_user_root(monkeypatch):
    monkeypatch.delenv("SJTUCLAW_DATA_DIR", raising=False)
    monkeypatch.delenv("SJTUCLAW_USER_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("C:/Users/tester")))
    monkeypatch.setattr(paths, "is_frozen", lambda: True)

    assert paths.data_dir() == Path("C:/Users/tester/.sjtuclaw/data")


def test_source_main_dir_is_project_root_even_when_cwd_differs(monkeypatch, tmp_path):
    project_root = tmp_path / "checkout"
    launch_dir = tmp_path / "webui"
    project_root.mkdir()
    launch_dir.mkdir()
    monkeypatch.setattr(paths, "is_frozen", lambda: False)
    monkeypatch.setattr(paths, "resource_root", lambda: project_root)
    monkeypatch.chdir(launch_dir)

    assert paths.main_dir() == project_root


def test_frozen_main_dir_is_hidden_user_directory(monkeypatch):
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "user_root", lambda: Path("C:/Users/tester/.sjtuclaw"))

    assert paths.main_dir() == Path("C:/Users/tester/.sjtuclaw")
