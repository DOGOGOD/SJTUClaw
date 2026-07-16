"""Skill manager tool: ``skill_manage`` — LLM-driven skill creation & editing.

Allows the agent to create, update, and delete skills, turning successful
approaches into reusable procedural knowledge. All skill files live under
the project ``skills/`` directory.

Actions:
  create      — Create a new skill (SKILL.md + directory structure)
  edit        — Replace the SKILL.md content of an existing skill (full rewrite)
  patch       — Targeted find-and-replace within SKILL.md or a supporting file
  delete      — Remove a skill directory (moves to .archive/ for recoverability)
  write_file  — Add/overwrite a supporting file (references, templates, assets)
  remove_file — Remove a supporting file from a skill

Directory layout:
    skills/
    ├── course-report/
    │   ├── SKILL.md
    │   ├── references/
    │   ├── templates/
    │   └── assets/
    └── my-skill/
        └── SKILL.md
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from claw.config import PROJECT_ROOT
from claw.paths import skills_dir
from claw.tools.base import Tool, ToolResult

SKILLS_DIR = skills_dir()
ARCHIVE_DIR = SKILLS_DIR / ".archive"

# Characters allowed in skill names (filesystem-safe)
_VALID_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]*$')
_MAX_NAME_LEN = 64
_MAX_CONTENT_CHARS = 100_000

# Subdirectories allowed for write_file/remove_file
_ALLOWED_SUBDIRS = {"references", "templates", "assets"}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_name(name: str) -> str | None:
    if not name:
        return "Skill 名称不能为空"
    if len(name) > _MAX_NAME_LEN:
        return f"Skill 名称超过 {_MAX_NAME_LEN} 个字符"
    if not _VALID_NAME_RE.match(name):
        return (
            f"无效的 Skill 名称 '{name}'。只允许小写字母、数字、"
            "连字符、点和下划线，且必须以字母或数字开头。"
        )
    return None


def _validate_frontmatter(content: str, expected_name: str | None = None) -> str | None:
    if not content.strip():
        return "SKILL.md 内容不能为空"
    if not content.startswith("---"):
        return "SKILL.md 必须以 YAML frontmatter (---) 开头"
    try:
        from claw.skills.registry import parse_frontmatter
        parsed = parse_frontmatter(content)
    except Exception as exc:
        return f"SKILL.md frontmatter 无效: {exc}"
    name = str(parsed.get("name", "")).strip()
    description = str(parsed.get("description", "")).strip()
    if not name:
        return "SKILL.md frontmatter 缺少 name"
    if expected_name and name != expected_name:
        return f"SKILL.md 的 name 必须与 Skill 名称一致：期望 '{expected_name}'，实际 '{name}'"
    if not description:
        return "SKILL.md frontmatter 缺少 description"
    if not str(parsed.get("instructions", "")).strip():
        return "SKILL.md 的 frontmatter 之后必须有正文内容"
    return None


def _validate_file_path(file_path: str) -> str | None:
    if not file_path:
        return "file_path 不能为空"
    if ".." in file_path:
        return "file_path 不能包含 '..' 路径跳转"
    p = Path(file_path)
    # Allow "SKILL.md" as a special case (explicitly target the main skill file)
    if p.name == "SKILL.md" and len(p.parts) <= 2:
        return None
    parts = p.parts
    if not parts or parts[0] not in _ALLOWED_SUBDIRS:
        allowed = ", ".join(sorted(_ALLOWED_SUBDIRS))
        return f"文件必须在以下目录之一内: {allowed}。当前: '{file_path}'"
    if len(parts) < 2:
        return f"请提供完整文件路径，而非仅目录名。例如: '{parts[0]}/example.md'"
    return None


def _find_skill_dir(name: str) -> Path | None:
    """Find a skill directory by name under skills/ (flat + one level of category)."""
    if not SKILLS_DIR.exists():
        return None
    # Skip .archive/
    def _is_archived(p: Path) -> bool:
        try:
            p.resolve().relative_to(ARCHIVE_DIR.resolve())
            return True
        except ValueError:
            return False

    for entry in sorted(SKILLS_DIR.iterdir()):
        if not entry.is_dir() or _is_archived(entry):
            continue
        if entry.name == name:
            # Direct match — this dir itself is the skill
            if (entry / "SKILL.md").exists():
                return entry
        # Search one level deeper for categorized skills
        # (e.g. skills/devops/my-tool/)
        for sub in sorted(entry.iterdir()):
            if sub.is_dir() and sub.name == name and not _is_archived(sub):
                if (sub / "SKILL.md").exists():
                    return sub
    return None


def _atomic_write(file_path: Path, content: str) -> None:
    """Atomically write content to file (tmp + replace)."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = file_path.with_name(f".{file_path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(file_path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------


def _create_skill(name: str, content: str, category: str | None = None) -> dict:
    """Create a new skill with SKILL.md."""
    err = _validate_name(name)
    if err:
        return {"success": False, "error": err}
    err = _validate_frontmatter(content, name)
    if err:
        return {"success": False, "error": err}
    if len(content) > _MAX_CONTENT_CHARS:
        return {
            "success": False,
            "error": f"SKILL.md 内容过长 ({len(content)} 字符，上限 {_MAX_CONTENT_CHARS})",
        }

    if _find_skill_dir(name):
        return {"success": False, "error": f"Skill '{name}' 已存在"}

    if category:
        category = str(category).strip()
        if err := _validate_name(category):
            return {"success": False, "error": f"无效的分类名: {err}"}
        if _find_skill_dir(category):
            return {"success": False, "error": f"分类名 '{category}' 与已有 Skill 冲突"}

    skill_dir = SKILLS_DIR / category / name if category else SKILLS_DIR / name
    try:
        skill_dir.resolve().relative_to(SKILLS_DIR.resolve())
    except ValueError:
        return {"success": False, "error": "Skill 路径超出 skills 目录"}
    skill_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(skill_dir / "SKILL.md", content)

    return {
        "success": True,
        "message": f"Skill '{name}' 已创建",
        "path": _display_path(skill_dir),
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _edit_skill(name: str, content: str) -> dict:
    """Full rewrite of an existing skill's SKILL.md."""
    err = _validate_frontmatter(content, name)
    if err:
        return {"success": False, "error": err}
    if len(content) > _MAX_CONTENT_CHARS:
        return {"success": False, "error": f"SKILL.md 内容超过 {_MAX_CONTENT_CHARS} 字符上限"}

    skill_dir = _find_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' 不存在。使用 skills_list() 查看可用 Skill。"}

    skill_md = skill_dir / "SKILL.md"
    original = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    _atomic_write(skill_md, content)

    # Rollback on empty result
    if not skill_md.exists() or skill_md.stat().st_size == 0:
        if original:
            _atomic_write(skill_md, original)
        return {"success": False, "error": "写入失败，已回滚"}

    return {
        "success": True,
        "message": f"Skill '{name}' 已更新（全文替换）",
    }


def _patch_skill(
    name: str,
    old_string: str,
    new_string: str | None,
    file_path: str | None = None,
    replace_all: bool = False,
) -> dict:
    """Find-and-replace within SKILL.md or a supporting file."""
    if not old_string:
        return {"success": False, "error": "old_string 不能为空"}

    skill_dir = _find_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' 不存在"}

    target: Path
    if file_path:
        err = _validate_file_path(file_path)
        if err:
            return {"success": False, "error": err}
        target = skill_dir / file_path
        try:
            target.resolve().relative_to(skill_dir.resolve())
        except ValueError:
            return {"success": False, "error": f"路径越界: '{file_path}'"}
    else:
        target = skill_dir / "SKILL.md"

    if not target.exists():
        return {"success": False, "error": f"文件不存在: {target.relative_to(skill_dir)}"}

    content = target.read_text(encoding="utf-8")

    if replace_all:
        count = content.count(old_string)
        if count == 0:
            return {
                "success": False,
                "error": f"未找到匹配文本。请确认 old_string 与文件内容完全一致。",
                "file_preview": content[:500],
            }
        new_content = content.replace(old_string, new_string or "")
    else:
        count = content.count(old_string)
        if count == 0:
            return {
                "success": False,
                "error": f"未找到匹配文本。请确认 old_string 与文件内容完全一致。",
                "file_preview": content[:500],
            }
        if count > 1:
            return {
                "success": False,
                "error": (
                    f"找到 {count} 处匹配，请提供更精确的 old_string 以唯一定位，"
                    "或设置 replace_all=true 替换全部。"
                ),
            }
        new_content = content.replace(old_string, new_string or "")

    if target.name == "SKILL.md":
        err = _validate_frontmatter(new_content, name)
        if err:
            return {"success": False, "error": f"修补后 SKILL.md 无效: {err}"}
    _atomic_write(target, new_content)

    return {
        "success": True,
        "message": f"Skill '{name}' 已修补（{count} 处替换）",
    }


def _delete_skill(name: str) -> dict:
    """Archive a skill (move to .archive/ for recoverability)."""
    skill_dir = _find_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' 不存在"}

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_target = ARCHIVE_DIR / name
    if archive_target.exists():
        # Append timestamp to avoid collision
        from datetime import datetime
        from claw.utils import default_tz

        ts = datetime.now(default_tz()).strftime("%Y%m%d-%H%M%S")
        archive_target = ARCHIVE_DIR / f"{name}-{ts}"

    shutil.move(str(skill_dir), str(archive_target))

    return {
        "success": True,
        "message": f"Skill '{name}' 已归档到 .archive/（可恢复）",
    }


def _write_file_to_skill(name: str, file_path: str, file_content: str) -> dict:
    """Add or overwrite a supporting file."""
    if Path(file_path).as_posix() == "SKILL.md":
        return {"success": False, "error": "请使用 action='edit' 修改 SKILL.md"}
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' 不存在。请先用 action='create' 创建。"}

    target = skill_dir / file_path
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        return {"success": False, "error": f"路径越界: '{file_path}'"}

    if len(file_content.encode("utf-8")) > 1_048_576:
        return {"success": False, "error": "文件内容超过 1 MiB 上限"}

    _atomic_write(target, file_content)

    return {
        "success": True,
        "message": f"文件 '{file_path}' 已写入 Skill '{name}'",
        "path": str(target),
    }


def _remove_file_from_skill(name: str, file_path: str) -> dict:
    """Remove a supporting file."""
    if Path(file_path).as_posix() == "SKILL.md":
        return {"success": False, "error": "不能通过 remove_file 删除 SKILL.md；请使用 delete 归档 Skill"}
    err = _validate_file_path(file_path)
    if err:
        return {"success": False, "error": err}

    skill_dir = _find_skill_dir(name)
    if not skill_dir:
        return {"success": False, "error": f"Skill '{name}' 不存在"}

    target = skill_dir / file_path
    try:
        target.resolve().relative_to(skill_dir.resolve())
    except ValueError:
        return {"success": False, "error": f"路径越界: '{file_path}'"}

    if not target.exists():
        return {"success": False, "error": f"文件 '{file_path}' 不存在"}

    target.unlink()

    # Clean up empty parent dirs
    parent = target.parent
    if parent != skill_dir and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()

    return {
        "success": True,
        "message": f"文件 '{file_path}' 已从 Skill '{name}' 中删除",
    }


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def _make_skill_manage_handler(registry=None):
    """Factory that produces a handler with access to the SkillRegistry."""

    def handler(args: dict[str, Any]) -> ToolResult:
        return _dispatch_skill_manage(args, registry)

    return handler


def _dispatch_skill_manage(args: dict[str, Any], registry=None) -> ToolResult:
    """Dispatch to the appropriate action handler."""
    action = (args.get("action") or "").strip()
    name = (args.get("name") or "").strip()

    if not action:
        return ToolResult(ok=False, error="缺少 'action' 参数")
    if not name:
        return ToolResult(ok=False, error="缺少 'name' 参数")

    if action == "create":
        content = args.get("content") or ""
        if not content:
            return ToolResult(ok=False, error="缺少 'content' 参数")
        result = _create_skill(name, content, category=args.get("category"))

    elif action == "edit":
        content = args.get("content") or ""
        if not content:
            return ToolResult(ok=False, error="缺少 'content' 参数")
        result = _edit_skill(name, content)

    elif action == "patch":
        old = args.get("old_string") or ""
        new = args.get("new_string")  # None = empty string (remove)
        result = _patch_skill(
            name, old, new,
            file_path=args.get("file_path"),
            replace_all=bool(args.get("replace_all")),
        )

    elif action == "delete":
        result = _delete_skill(name)

    elif action == "write_file":
        fp = args.get("file_path") or ""
        fc = args.get("file_content")
        if not fp:
            return ToolResult(ok=False, error="缺少 'file_path' 参数")
        if fc is None:
            return ToolResult(ok=False, error="缺少 'file_content' 参数")
        result = _write_file_to_skill(name, fp, fc)

    elif action == "remove_file":
        fp = args.get("file_path") or ""
        if not fp:
            return ToolResult(ok=False, error="缺少 'file_path' 参数")
        result = _remove_file_from_skill(name, fp)

    else:
        return ToolResult(
            ok=False,
            error=f"未知 action '{action}'。可用: create, edit, patch, delete, write_file, remove_file",
        )

    if result.get("success"):
        # Rescan registry so new/changed skills appear immediately
        if registry is not None:
            try:
                registry.rescan(force=True)
            except Exception:
                pass

        # Record usage telemetry (best-effort)
        try:
            from claw.skills.usage import SkillUsageStore
            store = SkillUsageStore(SKILLS_DIR)
            if action == "delete":
                store.forget(name)
            elif action in ("patch", "edit", "write_file", "remove_file"):
                store.bump_patch(name)
        except Exception:
            pass

        return ToolResult(ok=True, content=json.dumps(result, ensure_ascii=False))
    else:
        return ToolResult(ok=False, error=json.dumps(result, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def create_skill_manage_tool(registry=None) -> Tool:
    """Create the ``skill_manage`` tool."""
    return Tool(
        name="skill_manage",
        description=(
            "管理 Skill（技能）——创建、更新、删除。Skill 是你的程序化记忆，"
            "用于保存可复用的任务操作方法。\n\n"
            "可用操作：\n"
            "- create: 创建新 Skill（需提供完整的 SKILL.md 内容，"
            "含 YAML frontmatter 和 Markdown 正文）\n"
            "- edit: 全文替换已有 Skill 的 SKILL.md\n"
            "- patch: 精确查找替换（推荐用于小修改，通过 old_string/new_string）\n"
            "- delete: 删除 Skill（移动到 .archive/，可恢复）\n"
            "- write_file: 向 Skill 添加 references/templates/assets 子文件\n"
            "- remove_file: 删除 Skill 的子文件\n\n"
            "何时创建 Skill：任务完成且方法值得复用、用户要求记住操作流程。"
            "创建前先与用户确认。修改前先用 skill_view() 读取当前内容。\n\n"
            "patch 的 old_string 必须精确定位（包含足够上下文保证唯一性），"
            "或用 replace_all=true 替换所有匹配项。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "edit", "patch", "delete", "write_file", "remove_file"],
                    "description": "要执行的操作类型",
                },
                "name": {
                    "type": "string",
                    "description": "Skill 名称（小写字母数字连字符，最长 64 字符）",
                },
                "content": {
                    "type": "string",
                    "description": "完整 SKILL.md 内容（create/edit 时必需）",
                },
                "category": {
                    "type": "string",
                    "description": "可选分类（create 时使用，如 'devops'）",
                },
                "old_string": {
                    "type": "string",
                    "description": "要查找的文本（patch 时必需）",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换文本（patch 时必需，可传空串删除匹配文本）",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "是否替换全部匹配项（默认 false，要求唯一匹配）",
                },
                "file_path": {
                    "type": "string",
                    "description": "子文件路径（write_file/remove_file 时必需，必须在 references/ templates/ assets/ 内）",
                },
                "file_content": {
                    "type": "string",
                    "description": "文件内容（write_file 时必需）",
                },
            },
            "required": ["action", "name"],
        },
        handler=_make_skill_manage_handler(registry),
        safety_level="write",
    )
