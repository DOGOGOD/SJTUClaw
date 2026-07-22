"""Run Pi's full coding agent behind SJTUClaw through official JSONL RPC."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from claw.agent.events import ErrorEvent, FinalEvent, ThinkingEvent, ToolCallEndEvent, ToolCallStartEvent
from claw.approval.manager import ApprovalRequest, ApprovalStatus
from claw.config import DATA_DIR, MAIN_DIR, PROJECT_ROOT, LLMConfig
from claw.llm.client import LLMClient
from claw.runtime_settings import setting_value
from claw.utils import now_iso

logger = logging.getLogger(__name__)


class PiError(RuntimeError):
    """Pi could not start or complete a turn."""


@dataclass(frozen=True)
class PiRuntimeConfig:
    command: tuple[str, ...]
    cwd: Path
    session_dir: Path
    provider: str = ""
    model: str = ""
    thinking: str = ""
    agent_dir: Path | None = None
    trust_tools: bool = False
    turn_timeout_s: float = 1800.0


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _default_pi_repo() -> Path:
    raw = setting_value("PI_REPO_DIR", "").strip()
    return Path(raw).expanduser().resolve() if raw else (PROJECT_ROOT.parent / "pi").resolve()


def _resolve_pi_command() -> tuple[str, ...]:
    raw = setting_value("PI_COMMAND", "").strip()
    if raw:
        return tuple(part.strip('"') for part in shlex.split(raw, posix=False))
    cli_raw = setting_value("PI_CLI_PATH", "").strip()
    node = setting_value("PI_NODE_PATH", "").strip() or shutil.which("node")
    cli = Path(cli_raw).expanduser().resolve() if cli_raw else _default_pi_repo() / "packages" / "coding-agent" / "dist" / "cli.js"
    if cli.is_file():
        if not node:
            raise PiError("已找到 Pi 构建产物，但找不到 Node.js；请设置 PI_NODE_PATH。")
        return (str(node), str(cli))
    installed = shutil.which("pi") or shutil.which("pi.cmd")
    if installed:
        return (installed,)
    raise PiError("找不到可运行的 Pi。请先构建相邻 pi 仓库，或设置 PI_COMMAND / PI_CLI_PATH。")


def load_pi_config() -> PiRuntimeConfig:
    cwd = setting_value("PI_CWD", "").strip()
    sessions = setting_value("PI_SESSION_DIR", "").strip()
    agent_dir = setting_value("PI_AGENT_DIR", "").strip()
    try:
        timeout = max(1.0, float(setting_value("PI_TURN_TIMEOUT_S", "1800")))
    except ValueError:
        timeout = 1800.0
    return PiRuntimeConfig(
        command=_resolve_pi_command(),
        cwd=Path(cwd).expanduser().resolve() if cwd else MAIN_DIR.resolve(),
        session_dir=Path(sessions).expanduser().resolve() if sessions else (DATA_DIR / "pi" / "sessions").resolve(),
        provider=setting_value("PI_PROVIDER", "").strip(),
        model=setting_value("PI_MODEL", "").strip(),
        thinking=setting_value("PI_THINKING", "").strip(),
        agent_dir=Path(agent_dir).expanduser().resolve() if agent_dir else None,
        trust_tools=_truthy(setting_value("PI_TRUST_TOOLS", "false")),
        turn_timeout_s=timeout,
    )


def _emit(callback: Callable[[Any], None] | None, event: Any) -> None:
    if callback is None:
        return
    if hasattr(event, "timestamp") and not event.timestamp:
        event.timestamp = now_iso()
    try:
        callback(event)
    except Exception:
        logger.exception("Pi 事件回调执行失败，已忽略")


def _session_token(session_id: str, generation: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{generation}".encode()).hexdigest()
    return f"sjtuclaw-{digest[:32]}"


class PiAgentClient(LLMClient):
    """LLM facade that delegates only complete main-agent turns to Pi."""

    def run_agent_turn(self, session_id: str, user_message: str, *, session_store,
                       approval_handler=None, media=None, event_callback=None,
                       cancel_event=None, input_event=None, rollback_message_id=None,
                       rollback_checkpoint_id=None, skill_source="", skill_name="", **_ignored) -> str:
        config = load_pi_config()
        config.session_dir.mkdir(parents=True, exist_ok=True)
        session = session_store.get(session_id)
        generation = str(session.metadata.get("pi_session_generation") or "1")
        session.metadata["pi_session_generation"] = generation
        message_args = dict(media=media, injected_event=input_event)
        if rollback_message_id:
            message_args.update(message_id=rollback_message_id, rollback_checkpoint_id=rollback_checkpoint_id)
        session.append_message("user", user_message, **message_args)
        session_store.save(session, fsync=True)

        prompt = f"/skill:{skill_name} {user_message}" if skill_source == "explicit" and skill_name else user_message
        started = time.monotonic()
        try:
            result = self._run_rpc(self._build_command(config, _session_token(session_id, generation)), config, prompt,
                                   media=media, session_id=session_id, approval_handler=approval_handler,
                                   event_callback=event_callback, cancel_event=cancel_event)
        except Exception as exc:
            logger.exception("Pi Agent 本轮执行失败")
            _emit(event_callback, ErrorEvent(error=str(exc)))
            result = f"Pi Agent 执行失败：{exc}"
        session = session_store.get(session_id)
        assistant = session.append_message("assistant", result)
        assistant.latency_ms = int((time.monotonic() - started) * 1000)
        session_store.save(session, fsync=True)
        _emit(event_callback, FinalEvent(content=result))
        return result

    @staticmethod
    def _build_command(config: PiRuntimeConfig, pi_session_id: str) -> list[str]:
        args = [*config.command, "--mode", "rpc", "--session-dir", str(config.session_dir), "--session-id", pi_session_id,
                "--extension", str(PROJECT_ROOT / "claw" / "pi" / "permission_gate.ts"),
                "--append-system-prompt", str(PROJECT_ROOT / "prompts" / "system_prompt.md"),
                "--append-system-prompt", str(PROJECT_ROOT / "prompts" / "soul.md")]
        if config.provider:
            args += ["--provider", config.provider]
        if config.model:
            args += ["--model", config.model]
        if config.thinking:
            args += ["--thinking", config.thinking]
        skills = PROJECT_ROOT / "skills"
        if skills.is_dir():
            for skill in sorted(skills.iterdir()):
                if (skill / "SKILL.md").is_file():
                    args += ["--skill", str(skill)]
        return args

    def _run_rpc(self, command: Sequence[str], config: PiRuntimeConfig, prompt: str, *, media, session_id,
                 approval_handler, event_callback, cancel_event) -> str:
        env = os.environ.copy()
        if config.agent_dir:
            env["PI_CODING_AGENT_DIR"] = str(config.agent_dir)
        proc = subprocess.Popen(list(command), cwd=str(config.cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        stderr: list[str] = []
        threading.Thread(target=self._collect_stderr, args=(proc, stderr), daemon=True).start()
        lock = threading.Lock()

        def send(payload):
            if proc.stdin is None:
                raise PiError("Pi RPC 标准输入不可用。")
            with lock:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()

        cancelled = threading.Event()
        def watch_cancel():
            if cancel_event is None:
                return
            cancel_event.wait()
            if proc.poll() is None:
                cancelled.set()
                try:
                    send({"id": "sjtu-abort", "type": "abort"})
                except (OSError, PiError):
                    pass
        threading.Thread(target=watch_cancel, daemon=True).start()
        payload = {"id": "sjtu-prompt", "type": "prompt", "message": prompt}
        images = self._encode_images(media or [])
        if images:
            payload["images"] = images
        send(payload)
        deadline, accepted, text, settled = time.monotonic() + config.turn_timeout_s, False, "", False
        try:
            if proc.stdout is None:
                raise PiError("Pi RPC 标准输出不可用。")
            while time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                try:
                    event = json.loads(line.rstrip("\r\n"))
                except json.JSONDecodeError:
                    continue
                kind = event.get("type")
                if kind == "response" and event.get("id") == "sjtu-prompt":
                    if not event.get("success"):
                        raise PiError(str(event.get("error") or "Pi 拒绝了 prompt"))
                    accepted = True
                elif kind == "extension_ui_request":
                    self._handle_ui_request(event, send, session_id=session_id, approval_handler=approval_handler,
                                            trust_tools=config.trust_tools)
                elif kind == "agent_start":
                    _emit(event_callback, ThinkingEvent(iteration=1))
                elif kind == "tool_execution_start":
                    _emit(event_callback, ToolCallStartEvent(call_id=str(event.get("toolCallId") or ""),
                          tool_name=str(event.get("toolName") or ""), args=event.get("args") or {}))
                elif kind == "tool_execution_end":
                    content = self._content_text((event.get("result") or {}).get("content"))
                    failed = bool(event.get("isError"))
                    _emit(event_callback, ToolCallEndEvent(call_id=str(event.get("toolCallId") or ""),
                          tool_name=str(event.get("toolName") or ""), ok=not failed,
                          result=None if failed else content, error=content if failed else None))
                elif kind == "message_update":
                    delta = event.get("assistantMessageEvent") or {}
                    if delta.get("type") == "text_delta":
                        text += str(delta.get("delta") or "")
                elif kind == "extension_error" or (kind == "auto_retry_end" and event.get("success") is False):
                    _emit(event_callback, ErrorEvent(error=str(event.get("error") or event.get("finalError") or "Pi 运行错误")))
                elif kind == "agent_settled":
                    settled = True
                    break
            if not settled:
                if cancelled.is_set():
                    return "本轮任务已由用户终止；Pi 已停止继续执行。"
                if time.monotonic() >= deadline:
                    raise PiError(f"Pi Agent 超过 {config.turn_timeout_s:g} 秒仍未完成。")
                raise PiError(f"Pi 进程提前退出（code={proc.poll()}）。{''.join(stderr)[-2000:].strip()}")
            if not accepted:
                raise PiError("Pi 未确认接收 prompt。")
            return text.strip() or "Pi 已完成本轮处理，但没有返回文本内容。"
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

    @staticmethod
    def _collect_stderr(proc, output):
        if proc.stderr:
            for line in proc.stderr:
                output.append(line)
                logger.debug("Pi: %s", line.rstrip())

    @staticmethod
    def _content_text(content):
        return "\n".join(str(item.get("text") or "") for item in content or []
                         if isinstance(item, dict) and item.get("type") == "text")

    @staticmethod
    def _encode_images(paths):
        result = []
        for raw in paths:
            path = Path(raw)
            mime = mimetypes.guess_type(path.name)[0] if path.is_file() else None
            if mime and mime.startswith("image/"):
                result.append({"type": "image", "data": base64.b64encode(path.read_bytes()).decode("ascii"), "mimeType": mime})
        return result

    @staticmethod
    def _handle_ui_request(event, send, *, session_id, approval_handler, trust_tools):
        method, request_id = event.get("method"), str(event.get("id") or "")
        if method not in {"select", "confirm", "input", "editor"} or not request_id:
            return
        if method != "confirm" or event.get("title") != "SJTUClaw 工具审批":
            send({"type": "extension_ui_response", "id": request_id, "cancelled": True})
            return
        try:
            payload = json.loads(str(event.get("message") or "{}"))
        except json.JSONDecodeError:
            payload = {}
        approved = trust_tools
        if not approved and approval_handler:
            request = ApprovalRequest(session_id=session_id, tool_name=str(payload.get("toolName") or "pi_tool"),
                                      tool_args=payload.get("input") if isinstance(payload.get("input"), dict) else {})
            try:
                approved = approval_handler(request).status == ApprovalStatus.APPROVED.value
            except Exception:
                logger.exception("Pi 工具审批失败，已安全拒绝")
        send({"type": "extension_ui_response", "id": request_id, "confirmed": approved})


def create_agent_client(config: LLMConfig) -> LLMClient:
    return PiAgentClient(config) if setting_value("AGENT_BACKEND", "sjtuclaw").strip().lower() == "pi" else LLMClient(config)
