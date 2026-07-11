"""Git tracking for durable memory files.

Tracks MEMORY.md, SOUL.md, and USER.md in a git repository so that
every Dream edit is version-controlled, auditable, and rollback-able.

Usage::

    tracker = GitTracker(workspace_root)
    tracker.init()
    tracker.commit("Dream: updated MEMORY.md with project context")
    tracker.log()          # recent commits
    tracker.rollback(-1)   # undo last commit
"""

from __future__ import annotations

import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Iterable


class GitTrackerError(RuntimeError):
    """Raised when a git operation fails."""


class GitTracker:
    """Lightweight git wrapper for memory file versioning.

    Files tracked: SOUL.md, USER.md, memory/MEMORY.md.

    All operations are best-effort — a missing git binary or
    uninitialized repo produces logged warnings, not crashes.
    """

    _TRACKED_FILES = ("SOUL.md", "USER.md", "memory/MEMORY.md")

    def __init__(self, workspace_root: Path):
        self._root = Path(workspace_root)
        self._available = self._check_git()

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @staticmethod
    def _check_git() -> bool:
        """Return True if git is available on PATH."""
        try:
            subprocess.run(
                ["git", "--version"],
                capture_output=True,
                check=True,
                timeout=5,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            return False

    def is_available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def init(self) -> bool:
        """Initialize a git repo in the workspace root if one doesn't exist.

        Returns True if successful.
        """
        if not self._available:
            return False

        if (self._root / ".git").exists():
            return True

        try:
            subprocess.run(
                ["git", "init"],
                cwd=str(self._root),
                capture_output=True,
                check=True,
                timeout=10,
            )
            # Create .gitignore if needed
            gitignore = self._root / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text(
                    "data/sessions/\n"
                    "data/tasks/\n"
                    "__pycache__/\n"
                    ".env\n"
                    "*.pyc\n"
                )
            return True
        except subprocess.CalledProcessError:
            return False

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    def commit(self, message: str, files: Iterable[str] | None = None) -> bool:
        """Stage and commit tracked files with *message*.

        If *files* is None, all tracked files are staged.
        Returns True if a commit was created (i.e. there were changes).
        """
        if not self._available:
            return False

        paths = list(files) if files else list(self._TRACKED_FILES)

        # Stage
        for p in paths:
            full = self._root / p
            if full.exists():
                try:
                    subprocess.run(
                        ["git", "add", str(full)],
                        cwd=str(self._root),
                        capture_output=True,
                        timeout=10,
                    )
                except subprocess.CalledProcessError:
                    pass

        # Check if there's anything to commit
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=str(self._root),
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                return False  # no changes
        except subprocess.CalledProcessError:
            pass

        # Commit
        try:
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(self._root),
                capture_output=True,
                check=True,
                timeout=10,
            )
            return True
        except subprocess.CalledProcessError:
            return False

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------

    def log(self, max_count: int = 10) -> list[dict[str, str]]:
        """Return recent commit history."""
        if not self._available or not (self._root / ".git").exists():
            return []

        try:
            result = subprocess.run(
                [
                    "git", "log",
                    f"--max-count={max_count}",
                    "--format=%H|%ai|%s",
                    "--", *self._TRACKED_FILES,
                ],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except subprocess.CalledProcessError:
            return []

        commits = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 2)
            if len(parts) >= 3:
                commits.append({
                    "hash": parts[0][:8],
                    "date": parts[1][:19],
                    "message": parts[2],
                })
        return commits

    # ------------------------------------------------------------------
    # Diff (working tree changes)
    # ------------------------------------------------------------------

    def diff(self) -> str:
        """Return a summary of uncommitted changes to tracked files."""
        if not self._available:
            return ""

        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "--", *self._TRACKED_FILES],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return ""

    def summarize_working_tree(self, paths: Iterable[str] | None = None) -> str:
        """Return a structured summary of working-tree changes.

        Used by Dream to ground commit messages in real file diffs.
        """
        paths = list(paths) if paths else list(self._TRACKED_FILES)

        if not self._available:
            # Fallback: list changed files with sizes
            lines = []
            for p in paths:
                full = self._root / p
                if full.exists():
                    size = full.stat().st_size
                    lines.append(f"{p}: {size} bytes")
            return "\n".join(lines) if lines else ""

        try:
            result = subprocess.run(
                ["git", "diff", "--stat", "--", *paths],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            return ""

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def rollback(self, n: int = -1) -> bool:
        """Rollback the last N commits that touched tracked files.

        ``n=-1`` means "undo the last commit", ``n=-2`` means "undo last 2".

        Returns True on success.
        """
        if not self._available or not (self._root / ".git").exists():
            return False

        try:
            # Find the commit N steps back
            result = subprocess.run(
                [
                    "git", "log",
                    "--format=%H",
                    "--max-count=1",
                    f"--skip={abs(n) - 1}",
                    "--", *self._TRACKED_FILES,
                ],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                timeout=10,
            )
            target = result.stdout.strip()
            if not target:
                return False

            # Checkout those files from the target commit
            for f in self._TRACKED_FILES:
                full = self._root / f
                if full.exists() or True:  # checkout will restore it
                    subprocess.run(
                        ["git", "checkout", target, "--", f],
                        cwd=str(self._root),
                        capture_output=True,
                        timeout=10,
                    )

            return True
        except subprocess.CalledProcessError:
            return False
