from __future__ import annotations

import io
import zipfile

import pytest


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            payload = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, payload)
    return buffer.getvalue()


def _skill_md(name: str = "demo-skill") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Demo skill\n"
        "---\n"
        "Use this skill for tests.\n"
    )


def test_install_and_remove_skill_package(tmp_path, monkeypatch):
    from claw.skills import management

    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(management, "SKILLS_DIR", tmp_path / "skills")

    data = _zip_bytes({
        "demo-skill/SKILL.md": _skill_md(),
        "demo-skill/references/example.md": "reference",
    })

    installed = management.install_skill_package_bytes(data, "demo.zip")
    assert installed["name"] == "demo-skill"
    assert (tmp_path / "skills" / "demo-skill" / "SKILL.md").is_file()

    removed = management.remove_skill_completely("demo-skill")
    assert removed["name"] == "demo-skill"
    assert not (tmp_path / "skills" / "demo-skill").exists()


def test_rejects_zip_path_traversal(tmp_path, monkeypatch):
    from claw.skills import management

    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(management, "SKILLS_DIR", tmp_path / "skills")

    data = _zip_bytes({
        "../escape.txt": "nope",
        "demo-skill/SKILL.md": _skill_md(),
    })

    with pytest.raises(management.SkillPackageError):
        management.validate_skill_package_bytes(data, "bad.zip")


def test_rejects_invalid_skill_content(tmp_path, monkeypatch):
    from claw.skills import management

    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(management, "SKILLS_DIR", tmp_path / "skills")

    data = _zip_bytes({"bad-skill/SKILL.md": "---\nname: bad-skill\n---\n"})

    with pytest.raises(management.SkillPackageError, match="description"):
        management.validate_skill_package_bytes(data, "bad.zip")


def test_rejects_existing_categorized_skill_without_replace(tmp_path, monkeypatch):
    from claw.skills import management

    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(management, "SKILLS_DIR", tmp_path / "skills")
    categorized = tmp_path / "skills" / "custom" / "demo-skill"
    categorized.mkdir(parents=True)
    (categorized / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    data = _zip_bytes({"demo-skill/SKILL.md": _skill_md()})

    with pytest.raises(management.SkillPackageError, match="已存在"):
        management.install_skill_package_bytes(data, "demo.zip")


def test_replace_existing_categorized_skill_removes_old_location(tmp_path, monkeypatch):
    from claw.skills import management

    monkeypatch.setattr(management, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(management, "SKILLS_DIR", tmp_path / "skills")
    categorized = tmp_path / "skills" / "custom" / "demo-skill"
    categorized.mkdir(parents=True)
    (categorized / "SKILL.md").write_text(_skill_md(), encoding="utf-8")

    data = _zip_bytes({"demo-skill/SKILL.md": _skill_md()})
    installed = management.install_skill_package_bytes(data, "demo.zip", replace=True)

    assert installed["name"] == "demo-skill"
    assert not categorized.exists()
    assert (tmp_path / "skills" / "demo-skill" / "SKILL.md").is_file()
