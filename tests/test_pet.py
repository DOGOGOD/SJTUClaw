from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from claw.pet.catalog import PetCatalog, PetCatalogError
from claw.pet.app import (
    CELL_HEIGHT,
    IDLE_DURATION_MULTIPLIER,
    NON_IDLE_REPEAT_COUNT,
    PET_BASE_SCALE,
    _make_color_key_safe,
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


if __name__ == "__main__":
    unittest.main()
