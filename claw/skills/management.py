"""Safe WebUI skill package installation and removal."""

from __future__ import annotations

import io
import os
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from claw.config import PROJECT_ROOT
from claw.skills.registry import SKILLS_DIR, SkillRegistryError, parse_frontmatter

MAX_PACKAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_UNPACKED_BYTES = 50 * 1024 * 1024
MAX_FILE_COUNT = 500
MAX_FILE_BYTES = 5 * 1024 * 1024
ALLOWED_SUFFIXES = {
    "",
    ".css",
    ".csv",
    ".html",
    ".jinja",
    ".jpeg",
    ".jpg",
    ".json",
    ".md",
    ".png",
    ".py",
    ".svg",
    ".toml",
    ".txt",
    ".webp",
    ".yaml",
    ".yml",
}
ALLOWED_TOP_LEVEL_FILES = {"SKILL.md", "README.md", "LICENSE", "LICENSE.md"}
ALLOWED_DIRS = {"assets", "references", "templates"}


class SkillPackageError(RuntimeError):
    """Raised when a skill package cannot be safely installed."""


@dataclass
class PackageMember:
    name: str
    size: int
    is_dir: bool = False


@dataclass
class ValidatedPackage:
    skill_name: str
    description: str
    root_prefix: str
    members: list[PackageMember] = field(default_factory=list)


def validate_skill_package_bytes(data: bytes, filename: str) -> ValidatedPackage:
    """Validate a compressed skill package without installing it."""
    if not data:
        raise SkillPackageError("上传包为空")
    if len(data) > MAX_PACKAGE_BYTES:
        raise SkillPackageError("上传包超过 20 MiB 限制")

    lower_name = filename.lower()
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _validate_zip(data)
    if lower_name.endswith((".tar", ".tar.gz", ".tgz")):
        return _validate_tar(data)
    raise SkillPackageError("仅支持 .zip、.tar、.tar.gz、.tgz 压缩包")


def install_skill_package_bytes(
    data: bytes,
    filename: str,
    *,
    replace: bool = False,
) -> dict:
    """Validate and install a skill package under ``skills/``."""
    validated = validate_skill_package_bytes(data, filename)
    target_dir = SKILLS_DIR / validated.skill_name
    _ensure_child_path(target_dir, SKILLS_DIR)
    existing_dir = find_skill_dir(validated.skill_name)
    if existing_dir is not None:
        if not replace:
            raise SkillPackageError(f"Skill '{validated.skill_name}' 已存在")

    with tempfile.TemporaryDirectory(prefix="skill-upload-") as tmp:
        tmp_dir = Path(tmp)
        if zipfile.is_zipfile(io.BytesIO(data)):
            _extract_zip(data, tmp_dir)
        else:
            _extract_tar(data, tmp_dir)
        source_root = _locate_extracted_root(tmp_dir, validated.root_prefix)
        _validate_extracted_tree(source_root, validated.skill_name)
        staged_root = tmp_dir / f".staged-{validated.skill_name}"
        shutil.move(str(source_root), str(staged_root))
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        if existing_dir is not None:
            _safe_rmtree(existing_dir, SKILLS_DIR)
        shutil.move(str(staged_root), str(target_dir))

    return {
        "name": validated.skill_name,
        "description": validated.description,
        "fileCount": len([m for m in validated.members if not m.is_dir]),
        "path": _display_path(target_dir),
    }


def remove_skill_completely(name: str) -> dict:
    """Completely delete one skill directory and its usage data."""
    skill_dir = find_skill_dir(name)
    if skill_dir is None:
        raise SkillPackageError(f"Skill '{name}' 不存在")
    _safe_rmtree(skill_dir, SKILLS_DIR)

    parent = skill_dir.parent
    if parent != SKILLS_DIR and parent.exists() and parent.parent == SKILLS_DIR:
        try:
            if not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    try:
        from claw.skills.usage import SkillUsageStore
        SkillUsageStore(SKILLS_DIR).forget(name)
    except Exception:
        pass
    return {"name": name, "message": f"Skill '{name}' 已彻底删除"}


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def find_skill_dir(name: str) -> Path | None:
    """Find a loaded skill directory by directory name."""
    if not _valid_skill_name(name) or not SKILLS_DIR.exists():
        return None
    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if entry.name == name and (entry / "SKILL.md").is_file():
            return entry
        for child in sorted(entry.iterdir()):
            if child.is_dir() and child.name == name and (child / "SKILL.md").is_file():
                return child
    return None


def _validate_zip(data: bytes) -> ValidatedPackage:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad:
                raise SkillPackageError(f"压缩包完整性校验失败: {bad}")
            members = [
                PackageMember(info.filename, int(info.file_size), info.is_dir())
                for info in zf.infolist()
            ]
            _validate_members(members)
            skill_md_name = _find_skill_md_member(members)
            raw = zf.read(skill_md_name).decode("utf-8")
            return _validate_skill_md(raw, members, skill_md_name)
    except zipfile.BadZipFile as exc:
        raise SkillPackageError("ZIP 文件损坏或格式无效") from exc
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md 必须使用 UTF-8 编码") from exc


def _validate_tar(data: bytes) -> ValidatedPackage:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
            members = []
            for info in tf.getmembers():
                if info.issym() or info.islnk() or info.isdev():
                    raise SkillPackageError("压缩包不能包含符号链接、硬链接或设备文件")
                members.append(PackageMember(info.name, int(info.size), info.isdir()))
            _validate_members(members)
            skill_md_name = _find_skill_md_member(members)
            extracted = tf.extractfile(skill_md_name)
            if extracted is None:
                raise SkillPackageError("无法读取 SKILL.md")
            raw = extracted.read().decode("utf-8")
            return _validate_skill_md(raw, members, skill_md_name)
    except tarfile.TarError as exc:
        raise SkillPackageError("TAR 文件损坏或格式无效") from exc
    except UnicodeDecodeError as exc:
        raise SkillPackageError("SKILL.md 必须使用 UTF-8 编码") from exc


def _validate_members(members: list[PackageMember]) -> None:
    file_members = [m for m in members if not m.is_dir]
    if not file_members:
        raise SkillPackageError("压缩包内没有文件")
    if len(file_members) > MAX_FILE_COUNT:
        raise SkillPackageError(f"文件数量超过 {MAX_FILE_COUNT} 个")
    total = 0
    for member in members:
        rel = _clean_member_path(member.name)
        if member.is_dir:
            continue
        total += member.size
        if member.size > MAX_FILE_BYTES:
            raise SkillPackageError(f"单个文件超过 5 MiB: {rel}")
        if total > MAX_TOTAL_UNPACKED_BYTES:
            raise SkillPackageError("解压后总大小超过 50 MiB")
        suffix = PurePosixPath(rel).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            raise SkillPackageError(f"不允许的文件类型: {rel}")


def _validate_skill_md(
    raw: str,
    members: list[PackageMember],
    skill_md_name: str,
) -> ValidatedPackage:
    try:
        meta = parse_frontmatter(raw)
    except SkillRegistryError as exc:
        raise SkillPackageError(f"SKILL.md 内容无效: {exc}") from exc

    name = str(meta.get("name", "")).strip()
    description = str(meta.get("description", "")).strip()
    instructions = str(meta.get("instructions", "")).strip()
    if not _valid_skill_name(name):
        raise SkillPackageError("SKILL.md frontmatter 中的 name 格式无效")
    if not description:
        raise SkillPackageError("SKILL.md frontmatter 缺少 description")
    if not instructions:
        raise SkillPackageError("SKILL.md 正文不能为空")

    root_prefix = _root_prefix(skill_md_name)
    expected_skill_path = f"{root_prefix}SKILL.md"
    if skill_md_name != expected_skill_path:
        raise SkillPackageError("压缩包只能包含一个 skill 根目录")
    if root_prefix and root_prefix.rstrip("/") != name:
        raise SkillPackageError("skill 根目录名必须与 SKILL.md 的 name 一致")

    for member in members:
        rel = _clean_member_path(member.name)
        if member.is_dir:
            continue
        without_root = rel[len(root_prefix):] if root_prefix else rel
        parts = PurePosixPath(without_root).parts
        if not parts:
            continue
        if len(parts) == 1:
            if parts[0] not in ALLOWED_TOP_LEVEL_FILES:
                raise SkillPackageError(f"根目录不允许该文件: {without_root}")
        elif parts[0] not in ALLOWED_DIRS:
            allowed = ", ".join(sorted(ALLOWED_DIRS))
            raise SkillPackageError(f"子文件必须位于 {allowed} 内: {without_root}")

    return ValidatedPackage(
        skill_name=name,
        description=description,
        root_prefix=root_prefix,
        members=members,
    )


def _find_skill_md_member(members: Iterable[PackageMember]) -> str:
    matches = [
        _clean_member_path(m.name)
        for m in members
        if not m.is_dir and PurePosixPath(_clean_member_path(m.name)).name == "SKILL.md"
    ]
    if not matches:
        raise SkillPackageError("压缩包内必须包含 SKILL.md")
    if len(matches) > 1:
        raise SkillPackageError("压缩包内只能包含一个 SKILL.md")
    return matches[0]


def _root_prefix(member_name: str) -> str:
    parts = PurePosixPath(member_name).parts
    return "" if len(parts) == 1 else f"{parts[0]}/"


def _clean_member_path(name: str) -> str:
    raw = name.replace("\\", "/").lstrip("/")
    path = PurePosixPath(raw)
    if not raw or any(part in {"", ".", ".."} for part in path.parts):
        raise SkillPackageError(f"压缩包包含非法路径: {name}")
    if path.is_absolute():
        raise SkillPackageError(f"压缩包包含绝对路径: {name}")
    return path.as_posix()


def _extract_zip(data: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            rel = _clean_member_path(info.filename)
            dest = target / rel
            _ensure_child_path(dest, target)
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)


def _extract_tar(data: bytes, target: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for info in tf.getmembers():
            rel = _clean_member_path(info.name)
            dest = target / rel
            _ensure_child_path(dest, target)
            if info.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif info.isfile():
                extracted = tf.extractfile(info)
                if extracted is None:
                    raise SkillPackageError(f"无法读取文件: {rel}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with dest.open("wb") as out:
                    shutil.copyfileobj(extracted, out)


def _locate_extracted_root(tmp_dir: Path, root_prefix: str) -> Path:
    if root_prefix:
        return tmp_dir / root_prefix.rstrip("/")
    root = tmp_dir / "_root_skill"
    root.mkdir()
    for child in list(tmp_dir.iterdir()):
        if child == root:
            continue
        shutil.move(str(child), str(root / child.name))
    return root


def _validate_extracted_tree(root: Path, expected_name: str) -> None:
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise SkillPackageError("解压后未找到 SKILL.md")
    raw = skill_md.read_text(encoding="utf-8")
    meta = parse_frontmatter(raw)
    if str(meta.get("name", "")).strip() != expected_name:
        raise SkillPackageError("解压后 SKILL.md name 与校验结果不一致")


def _valid_skill_name(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name or ""))


def _ensure_child_path(path: Path, root: Path) -> None:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise SkillPackageError("路径越界，已拒绝操作") from exc


def _safe_rmtree(path: Path, root: Path) -> None:
    _ensure_child_path(path, root)
    if not path.exists():
        return
    if path.is_symlink() or not path.is_dir():
        raise SkillPackageError("目标不是合法 skill 目录")
    shutil.rmtree(path)
