from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile


class _Response:
    def __init__(self, data=None):
        self._data = data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _HTTPClient:
    def __init__(self):
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/files"):
            return _Response({"file_info": "uploaded-token"})
        return _Response()

    async def put(self, url, **kwargs):
        self.calls.append(("PUT", url, kwargs))
        return _Response()


def _authenticated_channel():
    from claw.channels.qq import QQChannel, QQConfig

    channel = QQChannel(QQConfig(app_id="app", client_secret="secret"))
    channel._access_token = "token"
    channel._token_expires_at = time.time() + 3600
    channel._http_client = _HTTPClient()
    return channel


def test_qq_approval_keyboard_and_interaction_ack():
    from claw.channels.qq_interactions import (
        build_approval_keyboard,
        parse_approval_button_data,
    )

    keyboard = build_approval_keyboard("apr_123")
    buttons = keyboard["content"]["rows"][0]["buttons"]
    assert parse_approval_button_data(buttons[0]["action"]["data"]) == (
        "apr_123",
        "approve",
    )

    channel = _authenticated_channel()
    received = []

    async def handler(event):
        received.append(event)

    channel.set_interaction_handler(handler)
    asyncio.run(channel._on_interaction({
        "id": "interaction-1",
        "chat_type": 1,
        "group_openid": "group-1",
        "group_member_openid": "member-1",
        "data": {"resolved": {"button_data": buttons[0]["action"]["data"]}},
    }))

    assert received[0].chat_id == "group-1"
    assert received[0].operator_id == "member-1"
    assert channel._http_client.calls[0][0] == "PUT"
    assert channel._http_client.calls[0][2]["json"] == {"code": 0}


def test_qq_sends_local_image_as_rich_media(tmp_path):
    from claw.channels.base import OutboundMessage

    image = tmp_path / "result.png"
    image.write_bytes(b"fake-png")
    channel = _authenticated_channel()
    asyncio.run(channel.send(OutboundMessage(
        chat_id="user-1",
        content="图片结果",
        media=[str(image)],
        metadata={"chat_type": "c2c", "message_id": "msg-1"},
    )))

    upload_call, send_call = channel._http_client.calls
    assert upload_call[1].endswith("/v2/users/user-1/files")
    assert upload_call[2]["json"]["file_type"] == 1
    assert upload_call[2]["json"]["file_data"]
    assert send_call[2]["json"]["msg_type"] == 7
    assert send_call[2]["json"]["media"] == {"file_info": "uploaded-token"}


def test_qq_dedup_records_new_ids_after_capacity_eviction(monkeypatch):
    import claw.channels.qq as qq_module

    monkeypatch.setattr(qq_module, "DEDUP_MAX_SIZE", 2)
    channel = qq_module.QQChannel(qq_module.QQConfig())

    assert channel._is_duplicate("first") is False
    assert channel._is_duplicate("second") is False
    assert channel._is_duplicate("third") is False
    assert channel._is_duplicate("third") is True
    assert len(channel._seen_messages) == 2


def test_qq_hello_clamps_untrusted_heartbeat_interval():
    from claw.channels.qq import QQChannel, QQConfig

    async def exercise():
        channel = QQChannel(QQConfig())

        async def identify():
            return None

        channel._send_identify = identify
        channel._dispatch_payload(
            {"op": 10, "d": {"heartbeat_interval": -1}}
        )
        await asyncio.sleep(0)
        return channel._heartbeat_interval

    assert asyncio.run(exercise()) == 0.8


def test_qq_dispatch_keeps_background_tasks_alive_until_completion():
    from claw.channels.qq import QQChannel, QQConfig

    async def exercise():
        channel = QQChannel(QQConfig())
        started = asyncio.Event()
        release = asyncio.Event()

        async def identify():
            started.set()
            await release.wait()

        channel._send_identify = identify
        channel._dispatch_payload({"op": 10, "d": {}})
        await started.wait()
        assert len(channel._background_tasks) == 1

        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not channel._background_tasks

    asyncio.run(exercise())


def test_qq_connect_reuses_socket_opened_by_reconnect():
    from claw.channels.qq import QQChannel, QQConfig

    class Socket:
        closed = False

    async def exercise():
        channel = QQChannel(QQConfig())
        channel._ws = Socket()
        opened = 0

        async def fail_if_opened(_url):
            nonlocal opened
            opened += 1

        async def read_events():
            return None

        async def heartbeat():
            return None

        channel._open_ws = fail_if_opened
        channel._read_events = read_events
        channel._heartbeat_loop = heartbeat
        await channel._connect_and_listen()
        await asyncio.sleep(0)
        return opened

    assert asyncio.run(exercise()) == 0


def test_concurrent_qq_messages_resolve_one_session(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_qq_session_map", {})

    with ThreadPoolExecutor(max_workers=12) as pool:
        session_ids = [
            future.result()
            for future in [
                pool.submit(
                    server._resolve_or_create_qq_session,
                    "same-chat",
                    "group",
                )
                for _ in range(40)
            ]
        ]

    assert len(set(session_ids)) == 1
    assert len(store.list_summaries()) == 1
    session = store.get(session_ids[0])
    assert session.metadata["qq_chat_id"] == "same-chat"


def test_web_attachment_image_is_persisted_as_message(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="image-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    upload = UploadFile(BytesIO(b"image-bytes"), filename="photo.png", headers={"content-type": "image/png"})
    result = asyncio.run(server.upload_attachment(session.session_id, upload))

    assert result["message"]["content"].startswith("![photo.png]")
    saved = store.get(session.session_id)
    assert saved.messages[-1]._command is True
    response = server.get_attachment_content(
        session.session_id, result["attachment"]["id"]
    )
    assert response.media_type == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_active_svg_attachment_is_never_rendered_inline(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="svg-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    upload = UploadFile(
        BytesIO(b"<svg><script>alert(1)</script></svg>"),
        filename="active.svg",
        headers={"content-type": "image/svg+xml"},
    )
    result = asyncio.run(server.upload_attachment(session.session_id, upload))

    assert result["message"]["content"].startswith("[active.svg]")
    response = server.get_attachment_content(
        session.session_id, result["attachment"]["id"]
    )
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_web_non_image_attachment_can_be_sent_to_agent(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="file-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    upload = UploadFile(
        BytesIO(b"plain-text"),
        filename="notes.txt",
        headers={"content-type": "text/plain"},
    )
    result = asyncio.run(
        server.upload_attachment(session.session_id, upload, persist_message=False)
    )

    assert result["message"]["content"].startswith("[notes.txt]")
    media_paths, markdown = server._resolve_chat_attachments(
        session.session_id, [result["attachment"]["id"]]
    )
    assert media_paths == []
    assert markdown == [
        f"[附件 notes.txt](/sessions/{session.session_id}/attachments/{result['attachment']['id']})"
    ]
    response = server.get_attachment_content(
        session.session_id, result["attachment"]["id"]
    )
    assert response.headers["content-disposition"].startswith("attachment;")


def test_concurrent_prompt_updates_keep_disk_and_runtime_in_sync(monkeypatch, tmp_path):
    from claw.gateway import server

    class _Builder:
        system_prompt = ""

        def update_system_prompt(self, content):
            self.system_prompt = content

    builder = _Builder()
    monkeypatch.setattr(server, "prompts_dir", lambda: tmp_path)
    monkeypatch.setattr(server, "_context_builder", builder)

    contents = [f"prompt-{index}" for index in range(30)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [
            pool.submit(
                server.update_system_prompt,
                server.UpdateContentRequest(content=content),
            )
            for content in contents
        ]
        for future in futures:
            future.result()

    persisted = (tmp_path / "system_prompt.md").read_text(encoding="utf-8")
    assert persisted == server._system_prompt == builder.system_prompt
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_attachment_metadata_updates_preserve_every_record(monkeypatch, tmp_path):
    from claw.gateway import server

    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    def add_record(index: int):
        return server._add_attachment_record(
            "attachment-race",
            f"att_{index}",
            f"image-{index}.png",
            f"att_{index}.png",
            index,
            "image/png",
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(add_record, index) for index in range(40)]
        for future in futures:
            future.result()

    records = server._read_attachments_meta("attachment-race")
    assert {record["id"] for record in records} == {
        f"att_{index}"
        for index in range(40)
    }


def test_web_attachment_markdown_escapes_bracketed_filename(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="bracket-image-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    upload = UploadFile(
        BytesIO(b"image-bytes"),
        filename="IMG_30[1].PNG",
        headers={"content-type": "image/png"},
    )
    result = asyncio.run(server.upload_attachment(session.session_id, upload))

    assert "![IMG_30&#91;1&#93;.PNG](" in result["message"]["content"]


def test_pending_web_image_is_not_persisted_until_chat_send(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="pending-image-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")

    upload = UploadFile(
        BytesIO(b"image-bytes"),
        filename="pasted.png",
        headers={"content-type": "image/png"},
    )
    result = asyncio.run(
        server.upload_attachment(session.session_id, upload, persist_message=False)
    )

    assert result["ok"] is True
    assert store.get(session.session_id).messages == []


def test_context_builder_sends_local_image_as_multimodal_content(tmp_path):
    from claw.context.builder import _multimodal_user_content

    image = tmp_path / "pasted.png"
    image.write_bytes(b"png-image-data")
    content = _multimodal_user_content("请描述图片", [str(image)])

    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "请描述图片"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_context_builder_skips_oversized_local_image(monkeypatch, tmp_path):
    import claw.context.builder as builder_module

    image = tmp_path / "oversized.png"
    image.write_bytes(b"12345")
    monkeypatch.setattr(builder_module, "_MAX_CONTEXT_IMAGE_BYTES", 4)

    content = builder_module._multimodal_user_content(
        "请描述图片", [str(image)]
    )

    assert content == "请描述图片"


def test_context_builder_only_replays_latest_user_images(tmp_path):
    from claw.context.builder import ContextBuilder
    from claw.memory.store import MemoryStore
    from claw.session.store import SessionStore

    old_image = tmp_path / "old.png"
    new_image = tmp_path / "new.png"
    old_image.write_bytes(b"old-image")
    new_image.write_bytes(b"new-image")
    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="image-history")
    session.append_message("user", "old", media=[str(old_image)])
    session.append_message("assistant", "seen")
    session.append_message("user", "new", media=[str(new_image)])
    builder = ContextBuilder("system", "soul", MemoryStore(tmp_path / "memory"))

    messages = builder.build_messages(session)
    image_urls = [
        block["image_url"]["url"]
        for message in messages
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "image_url"
    ]

    assert len(image_urls) == 1
    assert "bmV3LWltYWdl" in image_urls[0]


def test_chat_sends_uploaded_image_and_text_in_one_agent_turn(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.gateway.server import ChatRequest
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "session-data")
    session = store.create_session(session_id="combined-image-session")
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "SESSIONS_DIR", tmp_path / "session-data")
    monkeypatch.setattr(server, "_llm_ready", lambda *_args: True)
    monkeypatch.setattr(server, "auto_title_if_first_turn", lambda *args, **kwargs: None)

    upload = UploadFile(
        BytesIO(b"image-bytes"),
        filename="pasted.png",
        headers={"content-type": "image/png"},
    )
    uploaded = asyncio.run(
        server.upload_attachment(session.session_id, upload, persist_message=False)
    )
    captured = {}

    def _run_agent_turn(session_id, message, **kwargs):
        captured["message"] = message
        captured["media"] = kwargs.get("media")
        current = store.get(session_id)
        current.append_message("user", message, media=kwargs.get("media"))
        current.append_message("assistant", "我看到了图片")
        store.save(current)
        return "我看到了图片"

    monkeypatch.setattr(server, "run_agent_turn", _run_agent_turn)
    result = asyncio.run(server.handle_chat(ChatRequest(
        sessionId=session.session_id,
        message="这张图里有什么？",
        attachmentIds=[uploaded["attachment"]["id"]],
    )))

    assert result["reply"] == "我看到了图片"
    assert "这张图里有什么？" in captured["message"]
    assert "![pasted.png]" in captured["message"]
    assert len(captured["media"]) == 1
    assert store.get(session.session_id).messages[0].media == captured["media"]


def test_local_image_endpoint_rejects_outside_workspace(monkeypatch, tmp_path):
    from fastapi import HTTPException
    from claw.gateway import server
    from claw.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="workspace-session")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.png"
    inside.write_bytes(b"png")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")

    class _Workspace:
        def get(self, session_id):
            return workspace

    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_workspace_manager", _Workspace())
    assert server.get_local_image(session.session_id, str(inside)).media_type == "image/png"
    try:
        server.get_local_image(session.session_id, str(outside))
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("outside-workspace image was exposed")


def test_image_download_is_served_inline(tmp_path):
    from claw.gateway import server
    from claw.tools.download import register_download

    image = tmp_path / "heart.png"
    image.write_bytes(b"png")
    download_id = register_download(image)
    response = server.serve_download(download_id)

    assert response.media_type == "image/png"
    assert response.headers.get("content-disposition") is None


def test_image_download_button_requests_attachment_disposition(tmp_path):
    from claw.gateway import server
    from claw.tools.download import register_download

    image = tmp_path / "heart.png"
    image.write_bytes(b"png")
    download_id = register_download(image)
    response = server.serve_download(download_id, download=True)

    assert response.media_type == "image/png"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "heart.png" in response.headers["content-disposition"]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("image.png", b"png-bytes"),
        ("notes.txt", b"plain-text"),
        ("report.md", b"# report"),
        ("data.csv", b"name,value\nalpha,1\n"),
        ("payload.json", b'{"ok":true}'),
        ("document.pdf", b"%PDF-1.7"),
        ("document.docx", b"PK\x03\x04docx"),
        ("spreadsheet.xlsx", b"PK\x03\x04xlsx"),
        ("archive.zip", b"PK\x03\x04"),
    ],
)
def test_download_endpoint_serves_all_supported_file_formats(
    filename, content, tmp_path
):
    from claw.gateway import server
    from claw.tools import download

    download.configure_download_registry(None)
    file_path = tmp_path / filename
    file_path.write_bytes(content)
    download_id = download.register_download(file_path)
    suffix = "?download=1" if filename.endswith(".png") else ""
    try:
        with TestClient(server.app) as client:
            response = client.get(f"/downloads/{download_id}{suffix}")
        assert response.status_code == 200
        assert response.content == content
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert filename in disposition
    finally:
        download.configure_download_registry(None)


def test_old_chat_images_remain_available_after_gateway_restart(
    monkeypatch, tmp_path
):
    from claw.gateway import server
    from claw.tools import download

    registry = tmp_path / "downloads" / "registry.json"
    images = []
    for index in range(3):
        image = tmp_path / f"history-{index}.png"
        image.write_bytes(f"image-{index}".encode())
        images.append(image)

    try:
        download.configure_download_registry(registry)
        created_at = iter((10.0, 20.0, 30.0))
        monkeypatch.setattr(download.time, "time", lambda: next(created_at))
        download_ids = [download.register_download(image) for image in images]

        # Simulate reopening a chat after a restart, well past the former TTL.
        monkeypatch.setattr(download.time, "time", lambda: 100_000.0)
        download.configure_download_registry(None)
        download.configure_download_registry(registry)

        with TestClient(server.app) as client:
            responses = [
                client.get(f"/downloads/{download_id}")
                for download_id in download_ids
            ]

        assert [response.status_code for response in responses] == [200, 200, 200]
        assert [response.content for response in responses] == [
            image.read_bytes() for image in images
        ]
    finally:
        download.configure_download_registry(None)


def test_download_endpoint_recovers_link_pruned_by_older_version(
    monkeypatch, tmp_path
):
    import json
    from types import SimpleNamespace

    from claw.gateway import server
    from claw.tools import download

    download_id = "dl_123456789abc"
    image = tmp_path / "recovered.png"
    image.write_bytes(b"recovered-image")
    tool_message = SimpleNamespace(
        role="tool",
        name="create_download",
        content=json.dumps(
            {
                "tool": "create_download",
                "path": "recovered.png",
                "downloadId": download_id,
            }
        ),
    )
    session = SimpleNamespace(
        session_id="session-history",
        messages=[tool_message],
    )
    store = SimpleNamespace(
        list_summaries=lambda: [
            SimpleNamespace(session_id=session.session_id)
        ],
        get=lambda session_id: session,
    )

    class _Sandbox:
        def __init__(self):
            self.exported = []

        def should_use(self, session_id, workspace_manager):
            return True

        def export_file(self, session_id, workspace_manager, path):
            self.exported.append((session_id, path))
            return image

    sandbox = _Sandbox()
    monkeypatch.setattr(server, "_session_store", store)
    monkeypatch.setattr(server, "_sandbox_manager", sandbox)
    monkeypatch.setattr(server, "_workspace_manager", object())
    download.configure_download_registry(None)

    try:
        response = TestClient(server.app).get(f"/downloads/{download_id}")

        assert response.status_code == 200
        assert response.content == image.read_bytes()
        assert sandbox.exported == [("session-history", "recovered.png")]
        assert download.get_download(download_id) == image.resolve()
    finally:
        download.configure_download_registry(None)


def test_workspace_endpoint_normalizes_quotes_and_file_uri(monkeypatch, tmp_path):
    from pathlib import Path
    from claw.gateway import server

    class _Workspace:
        def set(self, session_id, path):
            assert session_id == "session-a"
            assert Path(path) == tmp_path
            return Path(path)

    monkeypatch.setattr(server, "_workspace_manager", _Workspace())
    request = server.SetWorkspaceRequest(
        sessionId="session-a", path=f'"file:///{str(tmp_path).replace(chr(92), "/")}"'
    )
    response = server.set_workspace(request)
    assert response["ok"] is True
    assert response["workspace"] == str(tmp_path)


def test_native_workspace_picker_enables_dpi_before_opening_tk(monkeypatch):
    import sys
    import types

    from claw.gateway import server

    calls = []

    class _FakeRoot:
        def withdraw(self):
            calls.append("withdraw")

        def attributes(self, *args):
            calls.append(("attributes", args))

        def destroy(self):
            calls.append("destroy")

    fake_filedialog = types.SimpleNamespace(
        askdirectory=lambda **kwargs: calls.append(("askdirectory", kwargs)) or r"C:\workspace"
    )
    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = lambda: calls.append("Tk") or _FakeRoot()
    fake_tk.filedialog = fake_filedialog

    monkeypatch.setattr(server, "_enable_native_dialog_dpi_awareness", lambda: calls.append("dpi"))
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_filedialog)

    path = server._pick_workspace_directory()

    assert path == r"C:\workspace"
    assert calls[0] == "dpi"
    assert calls[1] == "Tk"


def test_native_workspace_picker_runs_off_event_loop(monkeypatch):
    from claw.gateway import server

    monkeypatch.setattr(server, "_pick_workspace_directory", lambda: r"C:\Projects\demo")
    response = asyncio.run(server.pick_workspace_directory())
    assert response == {"ok": True, "cancelled": False, "path": r"C:\Projects\demo"}


def test_gateway_adds_inline_markdown_for_new_image_download(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore
    from claw.tools.download import register_download

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="download-session")
    session.append_message("assistant", "图片已生成")
    store.save(session)
    monkeypatch.setattr(server, "_session_store", store)
    image = tmp_path / "heart.png"
    image.write_bytes(b"png")
    before = set(server.list_downloads())
    register_download(image)

    reply = server._decorate_download_reply("download-session", "图片已生成", before)
    assert "![heart.png](/downloads/" in reply
    assert store.get("download-session").messages[-1].content == reply


def test_gateway_deduplicates_image_download_markdown(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore
    from claw.tools.download import register_download

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="duplicate-download-session")
    monkeypatch.setattr(server, "_session_store", store)
    image = tmp_path / "heart.png"
    image.write_bytes(b"png")
    before = set(server.list_downloads())
    download_id = register_download(image)
    duplicate_reply = (
        f"![heart.png](/downloads/{download_id})\n\n"
        f"[下载 heart.png](/downloads/{download_id})"
    )
    session.append_message("assistant", duplicate_reply)
    store.save(session)

    reply = server._decorate_download_reply(
        "duplicate-download-session", duplicate_reply, before
    )

    assert reply.count(f"/downloads/{download_id}") == 1
    assert reply.count("![heart.png]") == 1
    assert store.get("duplicate-download-session").messages[-1].content == reply


def test_gateway_adds_link_for_new_regular_download(monkeypatch, tmp_path):
    from claw.gateway import server
    from claw.session.store import SessionStore
    from claw.tools.download import register_download

    store = SessionStore(tmp_path / "sessions")
    session = store.create_session(session_id="download-file-session")
    session.append_message("assistant", "下载 ID 是 dl_demo")
    store.save(session)
    monkeypatch.setattr(server, "_session_store", store)
    report = tmp_path / "数据库索引性能实验报告.md"
    report.write_text("# report", encoding="utf-8")
    before = set(server.list_downloads())
    register_download(report)

    reply = server._decorate_download_reply(
        "download-file-session", "下载 ID 是 dl_demo", before
    )

    assert "[下载 数据库索引性能实验报告.md](/downloads/" in reply
    assert store.get("download-file-session").messages[-1].content == reply
