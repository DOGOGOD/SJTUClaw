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
    parser.add_argument(
        "--no-project-venv",
        action="store_true",
        help="disable /workspace/.venv for non-Python images",
    )
    parser.add_argument(
        "--verify-common-libs",
        action="store_true",
        help="verify the SJTUClaw common Python package set",
    )
    args = parser.parse_args()

    workspace = args.workspace.resolve() if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        parser.error(f"workspace is not a directory: {workspace}")

    config = load_sandbox_config()
    config = replace(
        config,
        mode="required",
        image=args.image or config.image,
        project_venv=(
            False if args.no_project_venv else config.project_venv
        ),
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
        if args.verify_common_libs:
            common = manager.run_command(
                session_id,
                workspace_manager,
                "python -c \"import bs4, docx, httpx, lxml, matplotlib, "
                "numpy, openpyxl, pandas, PIL, pypdf, reportlab, requests, "
                "scipy, seaborn, sklearn, yaml; "
                "print('common-libs-ok')\"",
                60,
            )
            if not common.ok or "common-libs-ok" not in common.stdout:
                raise RuntimeError(
                    common.stderr
                    or "sandbox common Python libraries are unavailable"
                )
        if config.project_venv:
            venv_probe = manager.run_command(
                session_id,
                workspace_manager,
                "test \"$(command -v python)\" = "
                "\"/opt/sjtuclaw/project-venv/bin/python\" && "
                "test \"$(command -v pip)\" = "
                "\"/opt/sjtuclaw/project-venv/bin/pip\" && "
                "test -z \"${PIP_PREFIX+x}\" && "
                "pip --version >/dev/null",
                60,
            )
            if not venv_probe.ok:
                raise RuntimeError(
                    venv_probe.stderr
                    or "project venv command routing failed"
                )
            install_probe = manager.run_command(
                session_id,
                workspace_manager,
                "python - <<'PY'\n"
                "from pathlib import Path\n"
                "from zipfile import ZIP_DEFLATED, ZipFile\n"
                "wheel = Path('/tmp/sjtuclaw_probe-0.0.1-"
                "py3-none-any.whl')\n"
                "with ZipFile(wheel, 'w', ZIP_DEFLATED) as archive:\n"
                "    archive.writestr("
                "'sjtuclaw_probe/__init__.py', "
                "\"VALUE = 20260729\\n\\ndef main():\\n"
                "    print('project-cli-persisted')\\n\")\n"
                "    archive.writestr("
                "'sjtuclaw_probe-0.0.1.dist-info/METADATA', "
                "'Metadata-Version: 2.1\\nName: sjtuclaw-probe\\n"
                "Version: 0.0.1\\n')\n"
                "    archive.writestr("
                "'sjtuclaw_probe-0.0.1.dist-info/WHEEL', "
                "'Wheel-Version: 1.0\\nGenerator: SJTUClaw smoke\\n"
                "Root-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
                "    archive.writestr("
                "'sjtuclaw_probe-0.0.1.dist-info/entry_points.txt', "
                "'[console_scripts]\\nsjtuclaw-probe="
                "sjtuclaw_probe:main\\n')\n"
                "    archive.writestr("
                "'sjtuclaw_probe-0.0.1.dist-info/RECORD', '')\n"
                "PY\n"
                "pip install --no-deps "
                "/tmp/sjtuclaw_probe-0.0.1-py3-none-any.whl",
                120,
            )
            if not install_probe.ok:
                raise RuntimeError(
                    install_probe.stderr
                    or "could not install local project package probe"
                )
            manager.close_session(session_id)
            manager.new_shell(session_id, workspace_manager)
            persisted = manager.run_command(
                session_id,
                workspace_manager,
                "python -c \"import sjtuclaw_probe as probe; "
                "assert probe.VALUE == 20260729; "
                "print('project-venv-persisted')\" && "
                "test \"$(command -v sjtuclaw-probe)\" = "
                "\"/opt/sjtuclaw/project-venv/bin/sjtuclaw-probe\" && "
                "sjtuclaw-probe",
                60,
            )
            if (
                not persisted.ok
                or "project-venv-persisted" not in persisted.stdout
                or "project-cli-persisted" not in persisted.stdout
            ):
                raise RuntimeError(
                    persisted.stderr
                    or "project venv did not persist across microVM restart"
                )
            removed = manager.run_command(
                session_id,
                workspace_manager,
                "pip uninstall -y sjtuclaw-probe >/dev/null",
                60,
            )
            if not removed.ok:
                raise RuntimeError(
                    removed.stderr
                    or "could not clean up project package probe"
                )
        else:
            persisted_file = manager.run_command(
                session_id,
                workspace_manager,
                "printf \"print('mount-persisted')\\n\" > "
                "/workspace/.sjtuclaw-mount-probe.py",
                60,
            )
            if not persisted_file.ok:
                raise RuntimeError(
                    persisted_file.stderr
                    or "could not write persistent mount probe"
                )
            manager.close_session(session_id)
            manager.new_shell(session_id, workspace_manager)
            persisted_file = manager.run_command(
                session_id,
                workspace_manager,
                "python /workspace/.sjtuclaw-mount-probe.py",
                60,
            )
            if (
                not persisted_file.ok
                or "mount-persisted" not in persisted_file.stdout
            ):
                raise RuntimeError(
                    persisted_file.stderr
                    or "workspace file did not survive microVM restart"
                )
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
