from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from claw.pet.catalog import PetCatalog, PetCatalogError
from claw.pet.app import (
    CELL_HEIGHT,
    IDLE_DURATION_MULTIPLIER,
    NON_IDLE_REPEAT_COUNT,
    PET_BASE_SCALE,
    DesktopPet,
    GatewayClient,
    _clear_pending_image,
    _make_color_key_safe,
    _point_in_bbox,
    _rounded_rectangle_points,
    _single_instance_lock,
    should_show_bubble,
)
from claw.pet.state import PetStateBroker


class PetCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        bundled = root / "bundled"
        pet_dir = bundled / "yuexinmiao"
        pet_dir.mkdir(parents=True)
        Image.new("RGBA", (1536, 1872), (0, 0, 0, 0)).save(
            pet_dir / "spritesheet.webp", "WEBP"
        )
        (pet_dir / "pet.json").write_text(
            json.dumps({
                "id": "yuexinmiao",
                "displayName": "月薪喵",
                "spritesheetPath": "spritesheet.webp",
            }),
            encoding="utf-8",
        )
        self.catalog = PetCatalog(root / "data", bundled)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_default_settings_and_position_persist(self):
        settings = self.catalog.load_settings()
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.selected_pet_id, "yuexinmiao")

        self.catalog.update_settings(position_x=123, position_y=456, update_position=True)
        reloaded = self.catalog.load_settings()
        self.assertEqual((reloaded.position_x, reloaded.position_y), (123, 456))

    def test_install_and_remove_v2_pet(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (1536, 2288), (0, 0, 0, 0)).save(buffer, "WEBP")
        buffer.seek(0)
        pet = self.catalog.install(
            pet_id="test-pet",
            display_name="Test Pet",
            description="test",
            spritesheet=buffer,
            filename="pet.webp",
            sprite_version_number=2,
        )
        self.assertEqual(pet["spriteVersionNumber"], 2)
        self.catalog.update_settings(selected_pet_id="test-pet")
        self.catalog.remove("test-pet")
        self.assertEqual(self.catalog.load_settings().selected_pet_id, "yuexinmiao")
        persisted = json.loads(
            (Path(self.tempdir.name) / "data" / "pet" / "settings.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted["selectedPetId"], "yuexinmiao")

    def _make_pet_package(
        self,
        *,
        pet_id: str = "zip-pet",
        root: str = "zip-pet",
        version: int = 2,
        height: int = 2288,
        extra_files: dict[str, bytes] | None = None,
        omit_first_frame: bool = False,
        fill_unused_frame: bool = False,
    ) -> io.BytesIO:
        spritesheet = io.BytesIO()
        atlas = Image.new("RGBA", (1536, height), (0, 0, 0, 0))
        frame_counts = (6, 8, 8, 4, 5, 8, 6, 6, 6, *((8, 8) if version == 2 else ()))
        for row, frame_count in enumerate(frame_counts):
            for column in range(frame_count):
                if omit_first_frame and row == 0 and column == 0:
                    continue
                left = column * 192 + 72
                top = row * 208 + 80
                atlas.paste((255, 120, 40, 255), (left, top, left + 48, top + 48))
        if fill_unused_frame:
            atlas.putpixel((7 * 192 + 96, 104), (255, 120, 40, 255))
        atlas.save(spritesheet, "WEBP", lossless=True)
        prefix = f"{root}/" if root else ""
        manifest = {
            "id": pet_id,
            "displayName": "ZIP Pet",
            "description": "installed from package",
            "spriteVersionNumber": version,
            "spritesheetPath": "spritesheet.webp",
        }
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{prefix}pet.json", json.dumps(manifest))
            archive.writestr(f"{prefix}spritesheet.webp", spritesheet.getvalue())
            for name, data in (extra_files or {}).items():
                archive.writestr(name, data)
        package.seek(0)
        return package

    def test_install_valid_zip_pet_package(self):
        pet = self.catalog.install_package(
            package=self._make_pet_package(),
            filename="zip-pet.zip",
        )

        self.assertEqual(pet["id"], "zip-pet")
        self.assertEqual(pet["displayName"], "ZIP Pet")
        self.assertEqual(pet["spriteVersionNumber"], 2)

    def test_install_pet_package_accepts_files_at_zip_root(self):
        pet = self.catalog.install_package(
            package=self._make_pet_package(pet_id="root-pet", root=""),
            filename="root-pet.zip",
        )
        self.assertEqual(pet["id"], "root-pet")

    def test_install_pet_package_rejects_unsafe_or_extra_members(self):
        for extra_name in ("../escape.txt", "zip-pet/readme.txt"):
            with self.subTest(extra_name=extra_name), self.assertRaises(PetCatalogError):
                self.catalog.install_package(
                    package=self._make_pet_package(extra_files={extra_name: b"no"}),
                    filename="zip-pet.zip",
                )

    def test_install_pet_package_rejects_mismatched_root_and_version(self):
        with self.assertRaisesRegex(PetCatalogError, "顶层目录名"):
            self.catalog.install_package(
                package=self._make_pet_package(root="wrong-root"),
                filename="zip-pet.zip",
            )
        with self.assertRaisesRegex(PetCatalogError, "尺寸不匹配"):
            self.catalog.install_package(
                package=self._make_pet_package(version=1),
                filename="zip-pet.zip",
            )

    def test_install_pet_package_rejects_empty_and_nontransparent_unused_frames(self):
        with self.assertRaisesRegex(PetCatalogError, "第 1 行第 1 帧为空"):
            self.catalog.install_package(
                package=self._make_pet_package(omit_first_frame=True),
                filename="zip-pet.zip",
            )
        with self.assertRaisesRegex(PetCatalogError, "未使用帧必须完全透明"):
            self.catalog.install_package(
                package=self._make_pet_package(fill_unused_frame=True),
                filename="zip-pet.zip",
            )

    def test_install_pet_package_rejects_invalid_zip_and_duplicate_id(self):
        with self.assertRaisesRegex(PetCatalogError, "有效的 ZIP"):
            self.catalog.install_package(package=io.BytesIO(b"not a zip"), filename="bad.zip")

        self.catalog.install_package(
            package=self._make_pet_package(),
            filename="zip-pet.zip",
        )
        with self.assertRaisesRegex(PetCatalogError, "已存在"):
            self.catalog.install_package(
                package=self._make_pet_package(),
                filename="zip-pet.zip",
            )

    def test_install_pet_package_rejects_invalid_spritesheet_image(self):
        package = io.BytesIO()
        manifest = {
            "id": "bad-image",
            "displayName": "Bad Image",
            "description": "",
            "spriteVersionNumber": 2,
            "spritesheetPath": "spritesheet.webp",
        }
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("bad-image/pet.json", json.dumps(manifest))
            archive.writestr("bad-image/spritesheet.webp", b"not an image")
        package.seek(0)

        with self.assertRaisesRegex(PetCatalogError, "有效的 PNG 或 WebP"):
            self.catalog.install_package(package=package, filename="bad-image.zip")

    def test_invalid_selected_pet_is_repaired_on_disk(self):
        settings_path = Path(self.tempdir.name) / "data" / "pet" / "settings.json"
        settings_path.write_text(
            json.dumps({"selectedPetId": "missing-pet"}),
            encoding="utf-8",
        )

        self.assertEqual(self.catalog.load_settings().selected_pet_id, "yuexinmiao")
        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["selectedPetId"], "yuexinmiao")

    def test_rejects_bad_atlas_dimensions(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (100, 100), (0, 0, 0, 0)).save(buffer, "PNG")
        buffer.seek(0)
        with self.assertRaises(PetCatalogError):
            self.catalog.install(
                pet_id="bad",
                display_name="Bad",
                description="",
                spritesheet=buffer,
                filename="bad.png",
            )

class PetStateBrokerTests(unittest.TestCase):
    def test_task_tool_approval_and_completion_projection(self):
        broker = PetStateBroker()
        broker.start_turn("s1", "修复登录问题")
        self.assertEqual(broker.snapshot()["animation"], "running")

        event = type("ToolCallStartEvent", (), {"tool_name": "shell"})()
        broker.handle_event("s1", event)
        self.assertIn("终端命令", broker.snapshot()["message"])

        broker.approval_pending("s1", "shell")
        self.assertEqual(broker.snapshot()["animation"], "waiting")
        broker.approval_resolved("s1", True)
        self.assertEqual(broker.snapshot()["phase"], "thinking")

        broker.finish_turn("s1")
        self.assertEqual(broker.snapshot()["animation"], "review")

    def test_desktop_lock_rejects_second_instance(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "pet.lock"
            with _single_instance_lock(path) as first:
                self.assertTrue(first)
                with _single_instance_lock(path) as second:
                    self.assertFalse(second)

    def test_bubble_is_hidden_only_when_idle_without_approval(self):
        self.assertFalse(should_show_bubble({"phase": "idle"}, []))
        self.assertTrue(should_show_bubble({"phase": "thinking"}, []))
        self.assertTrue(
            should_show_bubble({"phase": "idle"}, [{"approvalId": "apr_1"}])
        )

    def test_codex_animation_timing_and_display_size(self):
        self.assertEqual(round(CELL_HEIGHT * PET_BASE_SCALE), 121)
        self.assertEqual(IDLE_DURATION_MULTIPLIER, 6)
        self.assertEqual(NON_IDLE_REPEAT_COUNT, 3)

    def test_bubble_rounded_rectangle_points_stay_within_bounds(self):
        points = _rounded_rectangle_points(8, 8, 322, 72, 14)
        xs = points[0::2]
        ys = points[1::2]
        self.assertEqual((min(xs), max(xs)), (8, 322))
        self.assertEqual((min(ys), max(ys)), (8, 72))
        self.assertGreater(len(points), 8)

    def test_color_key_frames_have_no_translucent_edge_pixels(self):
        image = Image.new("RGBA", (3, 1))
        image.putdata([
            (80, 40, 20, 0),
            (80, 40, 20, 127),
            (80, 40, 20, 128),
        ])
        cleaned = _make_color_key_safe(image)
        alpha = cleaned.getchannel("A")
        self.assertEqual([alpha.getpixel((x, 0)) for x in range(3)], [0, 0, 255])

    def test_pending_image_remove_hit_area_includes_padding(self):
        badge_bounds = (16, 10, 56, 26)

        self.assertTrue(_point_in_bbox(12, 18, badge_bounds, padding=5))
        self.assertTrue(_point_in_bbox(61, 18, badge_bounds, padding=5))
        self.assertFalse(_point_in_bbox(62, 18, badge_bounds, padding=5))
        self.assertFalse(_point_in_bbox(20, 20, None, padding=5))

    def test_clearing_pending_image_restores_input_layout_and_focus(self):
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        pending_image = {"value": image}
        calls = []

        class _Canvas:
            def itemconfigure(self, item, **kwargs):
                calls.append(("itemconfigure", item, kwargs))

        class _Entry:
            def focus_set(self):
                calls.append(("focus_set",))

        _clear_pending_image(
            pending_image,
            _Canvas(),
            7,
            lambda: calls.append(("resize",)),
            _Entry(),
        )

        self.assertIsNone(pending_image["value"])
        self.assertEqual(calls, [
            ("itemconfigure", 7, {"text": ""}),
            ("resize",),
            ("focus_set",),
        ])


class GatewayClientImageUploadTests(unittest.TestCase):
    def test_clipboard_image_is_uploaded_as_png_multipart(self):
        captured = {}

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"ok": true}'

        def _urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return _Response()

        client = GatewayClient("http://127.0.0.1:8000")
        image = Image.new("RGB", (2, 2), (255, 0, 0))
        with patch("claw.pet.app.urllib.request.urlopen", side_effect=_urlopen):
            result = client.upload_image("session-a", image, "clipboard.png")

        request = captured["request"]
        self.assertTrue(result["ok"])
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:8000/sessions/session-a/attachments?persistMessage=false",
        )
        self.assertIn("multipart/form-data; boundary=", request.headers["Content-type"])
        self.assertIn(b'filename="clipboard.png"', request.data)
        self.assertIn(b"\x89PNG\r\n\x1a\n", request.data)
        self.assertEqual(captured["timeout"], 60.0)

    def test_desktop_pet_uploads_pending_image_then_sends_text_and_attachment_id(self):
        calls = []

        class _Client:
            def fetch_sessions(self):
                calls.append(("fetch_sessions",))
                return [{"sessionId": "session-a"}]

            def upload_image(self, session_id, image, filename):
                calls.append(("upload_image", session_id, image, filename))
                return {"ok": True, "attachment": {"id": "att_clipboard"}}

            def send_message(self, session_id, message, attachment_ids):
                calls.append(("send_message", session_id, message, attachment_ids))
                return {"ok": True, "reply": "图片分析完成"}

        pet = DesktopPet.__new__(DesktopPet)
        pet.client = _Client()
        replies = []
        pet._show_reply = replies.append
        pet.root = type("_Root", (), {"after": lambda _self, _delay, callback: callback()})()
        image = Image.new("RGB", (2, 2), (0, 128, 255))

        pet._send_message_worker("请分析图片", image)

        self.assertEqual(calls[0], ("fetch_sessions",))
        self.assertEqual(calls[1][0:2], ("upload_image", "session-a"))
        self.assertTrue(calls[1][3].startswith("clipboard-"))
        self.assertEqual(
            calls[2],
            ("send_message", "session-a", "请分析图片", ["att_clipboard"]),
        )
        self.assertEqual(replies, [{"ok": True, "reply": "图片分析完成"}])


if __name__ == "__main__":
    unittest.main()
