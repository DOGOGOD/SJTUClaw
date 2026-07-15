from __future__ import annotations

import asyncio
import time
from io import BytesIO

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
