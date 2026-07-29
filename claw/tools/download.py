"""Download tool (Step 8): create_download.

``create_download`` registers a workspace file for download via Gateway.
It does NOT return file content to the model — only a downloadId that
the frontend can use to retrieve the file.

Gateway stores these entries in a bounded, persistent registry. Entries remain
available while their backing files exist, so links in saved chat history keep
working across time and Gateway restarts.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from filelock import FileLock

from claw.config import DATA_DIR
from claw.tools.base import Tool, ToolResult
from claw.workspace.manager import WorkspaceManager, WorkspaceError

if TYPE_CHECKING:
    from claw.sandbox import SandboxManager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Download registry (shared with Gateway, optionally persisted)
# ---------------------------------------------------------------------------

_MAX_DOWNLOADS = 1_000
_downloads: OrderedDict[str, tuple[Path, float]] = OrderedDict()
"""downloadId -> (absolute Path, Unix creation time)."""
_downloads_lock = threading.Lock()
_download_registry_path: Path | None = None
_DOWNLOAD_ID_RE = re.compile(r"^dl_[0-9a-f]{12}$")


def is_valid_download_id(download_id: str) -> bool:
    """Return whether *download_id* uses the public opaque-id format."""
    return bool(_DOWNLOAD_ID_RE.fullmatch(download_id))


@contextmanager
def _registry_file_lock_locked() -> Iterator[None]:
    """Serialize registry read-modify-write cycles across Gateway processes."""
    registry_path = _download_registry_path
    if registry_path is None:
        yield
        return
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(registry_path) + ".lock", timeout=10):
        yield


def _prune_downloads_locked() -> bool:
    missing = [
        download_id
        for download_id, (path, _) in _downloads.items()
        if not path.is_file()
    ]
    for download_id in missing:
        _downloads.pop(download_id, None)
    trimmed = False
    while len(_downloads) > _MAX_DOWNLOADS:
        _, (path, _) = _downloads.popitem(last=False)
        _remove_managed_export(path)
        trimmed = True
    return bool(missing) or trimmed


def _remove_managed_export(path: Path) -> None:
    """Remove evicted sandbox exports without ever deleting workspace files."""
    export_root = (DATA_DIR / "sandbox" / "exports").resolve()
    try:
        resolved = path.resolve()
        resolved.relative_to(export_root)
    except (OSError, ValueError):
        return
    try:
        resolved.unlink(missing_ok=True)
    except OSError:
        logger.warning("无法清理已淘汰的 sandbox 导出文件: %s", resolved)
        return
    parent = resolved.parent
    while parent != export_root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _persist_downloads_locked() -> None:
    registry_path = _download_registry_path
    if registry_path is None:
        return
    payload = {
        "version": 1,
        "entries": [
            {
                "downloadId": download_id,
                "path": str(path),
                "createdAt": created_at,
            }
            for download_id, (path, created_at) in _downloads.items()
        ],
    }
    tmp_path = registry_path.with_name(
        f".{registry_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(registry_path)
    except OSError:
        logger.exception("无法持久化下载注册表: %s", registry_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _reload_downloads_locked() -> bool:
    """Refresh memory from disk and return whether disk needs normalization."""
    registry_path = _download_registry_path
    if registry_path is None:
        return False
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {}
    except (OSError, json.JSONDecodeError):
        logger.exception(
            "无法读取下载注册表，继续使用当前内存副本: %s",
            registry_path,
        )
        return False

    entries = payload.get("entries") if isinstance(payload, dict) else []
    normalized: OrderedDict[str, tuple[Path, float]] = OrderedDict()
    needs_persist = not isinstance(entries, list)
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                needs_persist = True
                continue
            download_id = str(item.get("downloadId") or "")
            raw_path = str(item.get("path") or "")
            try:
                created_at = float(item.get("createdAt"))
                path = Path(raw_path)
            except (TypeError, ValueError, OSError):
                needs_persist = True
                continue
            if not math.isfinite(created_at):
                needs_persist = True
                continue
            valid_id = bool(_DOWNLOAD_ID_RE.fullmatch(download_id))
            is_absolute = path.is_absolute()
            if valid_id and is_absolute and path.is_file():
                normalized[download_id] = (path.resolve(), created_at)
                continue
            needs_persist = True

    _downloads.clear()
    _downloads.update(normalized)
    return needs_persist


def configure_download_registry(registry_path: Path | None) -> None:
    """Configure persistence and reload downloads whose files still exist."""
    global _download_registry_path
    with _downloads_lock:
        _download_registry_path = (
            Path(registry_path).expanduser().resolve()
            if registry_path is not None
            else None
        )
        _downloads.clear()
        if _download_registry_path is None:
            return
        with _registry_file_lock_locked():
            _reload_downloads_locked()
            _prune_downloads_locked()
            _persist_downloads_locked()


def register_download(file_path: Path) -> str:
    """Create a download entry and return its id."""
    download_id = f"dl_{uuid.uuid4().hex[:12]}"
    restore_download(download_id, file_path)
    return download_id


def restore_download(download_id: str, file_path: Path) -> str:
    """Persist an existing download id after recovering its backing file."""
    if not is_valid_download_id(download_id):
        raise ValueError(f"下载 ID 格式无效: {download_id}")
    now = time.time()
    resolved = file_path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"下载文件不存在: {file_path}")
    with _downloads_lock:
        with _registry_file_lock_locked():
            _reload_downloads_locked()
            _prune_downloads_locked()
            _downloads[download_id] = (resolved, now)
            _prune_downloads_locked()
            _persist_downloads_locked()
    return download_id


def get_download(download_id: str) -> Path | None:
    """Return the file path for *download_id* or None."""
    if not is_valid_download_id(download_id):
        return None
    with _downloads_lock:
        with _registry_file_lock_locked():
            needs_persist = _reload_downloads_locked()
            if _prune_downloads_locked():
                needs_persist = True
            if needs_persist:
                _persist_downloads_locked()
            entry = _downloads.get(download_id)
            return entry[0] if entry is not None else None


def list_downloads() -> dict[str, str]:
    """Return {downloadId: file_name} for all active downloads."""
    with _downloads_lock:
        with _registry_file_lock_locked():
            needs_persist = _reload_downloads_locked()
            if _prune_downloads_locked():
                needs_persist = True
            if needs_persist:
                _persist_downloads_locked()
            return {did: entry[0].name for did, entry in _downloads.items()}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _make_create_download_handler(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
    sandbox_manager: SandboxManager | None = None,
) -> Callable[[dict[str, Any]], ToolResult]:
    def handler(args: dict[str, Any]) -> ToolResult:
        path_str: str = args["path"]
        session_id = session_id_provider()

        if sandbox_manager is not None:
            try:
                if sandbox_manager.should_use(session_id, workspace_manager):
                    resolved = sandbox_manager.export_file(
                        session_id, workspace_manager, path_str
                    )
                else:
                    resolved = workspace_manager.resolve(
                        session_id, path_str, must_exist=True
                    )
            except Exception as exc:
                return ToolResult(ok=False, error=str(exc))
        else:
            try:
                resolved = workspace_manager.resolve(
                    session_id, path_str, must_exist=True
                )
            except WorkspaceError as exc:
                return ToolResult(ok=False, error=str(exc))

        if not resolved.is_file():
            return ToolResult(
                ok=False,
                error=f"create_download 失败：路径不是文件 \"{path_str}\"",
            )

        try:
            download_id = register_download(resolved)
        except Exception as exc:
            return ToolResult(
                ok=False,
                error=f"create_download 失败：{exc}",
            )

        is_inline_image = resolved.suffix.lower() in {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".avif"
        }
        display_markdown = (
            f"![{resolved.name}](/downloads/{download_id})"
            if is_inline_image
            else f"[下载 {resolved.name}](/downloads/{download_id})"
        )
        return ToolResult(
            ok=True,
            content=json.dumps(
                {
                    "tool": "create_download",
                    "path": path_str,
                    "downloadId": download_id,
                    "fileName": resolved.name,
                    "downloadMarkdown": (
                        f"[下载 {resolved.name}](/downloads/{download_id})"
                    ),
                    "displayMarkdown": display_markdown,
                    "inlineMarkdown": (
                        f"![{resolved.name}](/downloads/{download_id})"
                        if is_inline_image
                        else None
                    ),
                    "result": "下载入口已创建",
                },
                ensure_ascii=False,
            ),
        )

    return handler


# ---------------------------------------------------------------------------
# Tool definition factory
# ---------------------------------------------------------------------------


def create_download_tool(
    workspace_manager: WorkspaceManager,
    session_id_provider: Callable[[], str],
    sandbox_manager: SandboxManager | None = None,
) -> Tool:
    return Tool(
        name="create_download",
        description=(
            "为 workspace 内已有文件创建一个可通过 Gateway 下载的临时入口。"
            "需要提供 path 参数（相对于 workspace 的文件路径）。"
            "文件必须已存在。返回 downloadId 和应直接展示的 displayMarkdown。"
            "最终回复只使用一次 displayMarkdown；图片的 displayMarkdown 已同时"
            "提供预览和前端下载按钮，不要再输出 downloadMarkdown 或 inlineMarkdown，"
            "也不要只把 downloadId 作为普通文字告诉用户。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "要创建下载入口的文件路径（相对于 workspace）",
                    "minLength": 1,
                }
            },
            "required": ["path"],
        },
        handler=_make_create_download_handler(
            workspace_manager, session_id_provider, sandbox_manager
        ),
        safety_level="download",
    )
