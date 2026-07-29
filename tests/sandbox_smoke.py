"""Manual real-microVM smoke test for SJTUClaw's sandbox integration."""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import replace
from pathlib import Path

from claw.sandbox import SandboxManager, load_sandbox_config
from claw.utils import force_utf8_stdio


class _Workspace:
    def __init__(self, root: Path | None) -> None:
        self.root = root

    def get(self, _session_id: str) -> Path | None:
        return self.root

    def is_unlimited(self, _session_id: str) -> bool:
        return False


def main() -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", help="override SANDBOX_IMAGE")
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args()

    workspace = args.workspace.resolve() if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")

    config = load_sandbox_config()
    config = replace(
        config,
        mode="required",
        image=args.image or config.image,
    )
    manager = SandboxManager(config)
    manager.set_agent_backend_provider(lambda _sid: "sjtuclaw")
    workspace_manager = _Workspace(workspace)
    session_id = f"smoke-{uuid.uuid4().hex}"
    export: Path | None = None

    try:
        shell = manager.new_shell(session_id, workspace_manager)
        command = manager.run_command(
            session_id,
            workspace_manager,
            "printf 'microvm-ok\\n'; uname -s; pwd",
            60,
        )
        if not command.ok:
            raise RuntimeError(command.stderr or f"exit code {command.exit_code}")
        binary = manager.run_command(
            session_id,
            workspace_manager,
            "printf '\\377'",
            60,
        )
        if not binary.ok or "\ufffd" not in binary.stdout:
            raise RuntimeError("non-UTF-8 shell output handling failed")
        timed = manager.run_command(
            session_id,
            workspace_manager,
            "sleep 5",
            1,
        )
        if not timed.timed_out:
            raise RuntimeError("sandbox shell timeout handling failed")
        limited = manager.run_command(
            session_id,
            workspace_manager,
            "yes x",
            30,
        )
        if (
            not limited.output_limited
            or not limited.stdout_truncated
            or len(limited.stdout.encode("utf-8")) > 128 * 1024
        ):
            raise RuntimeError("sandbox shell output limit handling failed")
        manager.overwrite_file(
            session_id,
            workspace_manager,
            ".sjtuclaw-sandbox-smoke.txt",
            "old-content\n",
        )
        manager.overwrite_file(
            session_id,
            workspace_manager,
            ".sjtuclaw-sandbox-smoke.txt",
            "filesystem-ok\n",
        )
        payload, truncated = manager.read_file(
            session_id,
            workspace_manager,
            ".sjtuclaw-sandbox-smoke.txt",
        )
        if truncated or payload != b"filesystem-ok\n":
            raise RuntimeError("sandbox filesystem round-trip failed")
        if (
            workspace is not None
            and (workspace / ".sjtuclaw-sandbox-smoke.txt").read_bytes()
            != b"filesystem-ok\n"
        ):
            raise RuntimeError("host-mounted atomic replacement failed")
        export = manager.export_file(
            session_id,
            workspace_manager,
            ".sjtuclaw-sandbox-smoke.txt",
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "shell": shell,
                    "stdout": command.stdout,
                    "binaryOutputDecoded": binary.stdout,
                    "export": str(export),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        manager.purge_session(session_id)
        manager.close_all()
        if export is not None:
            export.unlink(missing_ok=True)
            try:
                export.parent.rmdir()
            except OSError:
                pass
        if workspace is not None:
            (workspace / ".sjtuclaw-sandbox-smoke.txt").unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
