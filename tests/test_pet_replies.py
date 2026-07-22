from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from claw.pet.app import DesktopPet
from claw.pet.replies import (
    PetReplyStore,
    generate_and_store_pet_replies,
    parse_generated_replies,
)


class PetReplyStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.store = PetReplyStore(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_replies_are_persisted_in_independent_pet_files(self):
        self.store.save(
            pet_id="coding-cat",
            description="冷静的程序员猫",
            replies=["这个问题，让我先想想。", "别急，我在检查啦。"],
            source="llm",
        )
        self.store.save(
            pet_id="sunny-dog",
            description="精力充沛的小狗",
            replies=["出发！一起完成任务！"],
            source="llm",
        )

        self.assertEqual(
            self.store.load("coding-cat"),
            ("这个问题，让我先想想。", "别急，我在检查啦。"),
        )
        self.assertEqual(self.store.load("sunny-dog"), ("出发！一起完成任务！",))
        reply_dir = self.root / "pet" / "replies"
        self.assertTrue((reply_dir / "coding-cat.json").is_file())
        self.assertTrue((reply_dir / "sunny-dog.json").is_file())

        document = json.loads(
            (reply_dir / "coding-cat.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["petId"], "coding-cat")
        self.assertEqual(document["description"], "冷静的程序员猫")

    def test_invalid_or_mismatched_documents_are_not_loaded(self):
        path = self.root / "pet" / "replies" / "coding-cat.json"
        path.write_text("not-json", encoding="utf-8")
        self.assertEqual(self.store.load("coding-cat"), ())

        path.write_text(
            json.dumps({"petId": "another-pet", "replies": ["不应读取"]}),
            encoding="utf-8",
        )
        self.assertEqual(self.store.load("coding-cat"), ())

    def test_llm_generation_uses_description_and_saves_clean_replies(self):
        client = Mock(configured=True)
        client.chat.return_value = json.dumps([
            "先别催，让我优雅地想一想。",
            "代码会说话，我正在听。",
            "再戳一下，灵感就来啦。",
            "这个报错有点意思。",
            "交给我，问题不大。",
            "让我把思路理顺。",
            "今天也要写漂亮代码。",
            "等等，我发现线索了。",
            "放心，我还在盯着呢。",
            "喝口水，再继续吧。",
            "这个按钮可不是玩具。",
            "任务收到，马上出发。",
        ], ensure_ascii=False)
        pet = {
            "id": "coding-cat",
            "displayName": "代码猫",
            "description": "冷静、专业，偶尔说程序员冷笑话。",
        }

        result = generate_and_store_pet_replies(pet, client, self.store)

        self.assertEqual(result.source, "llm")
        self.assertEqual(len(result.replies), 12)
        self.assertEqual(self.store.load("coding-cat"), result.replies)
        messages = client.chat.call_args.args[0]
        self.assertIn(pet["description"], messages[1]["content"])
        system_prompt = messages[0]["content"]
        self.assertIn("每条回复都必须至少体现一项", system_prompt)
        self.assertIn("让人不看名字也能辨认这个角色", system_prompt)
        self.assertIn("不得杜撰 description 未支持", system_prompt)
        self.assertIn("不要写旁白、动作描写", system_prompt)

    def test_generation_failure_stores_pet_bound_fallback(self):
        client = Mock(configured=True)
        client.chat.side_effect = RuntimeError("provider unavailable")
        pet = {"id": "quiet-fox", "displayName": "安静狐", "description": "沉稳"}

        result = generate_and_store_pet_replies(pet, client, self.store)

        self.assertEqual(result.source, "fallback")
        self.assertTrue(result.warning)
        self.assertEqual(self.store.load("quiet-fox"), result.replies)
        self.assertIn("安静狐", result.replies[0])


class PetReplyParsingTests(unittest.TestCase):
    def test_parses_code_fence_deduplicates_and_rejects_non_strings(self):
        raw = """```json
["第一句", "第一句", 3, " 第二句 ", "第三句", "第四句", "第五句", "第六句"]
```"""
        self.assertEqual(
            parse_generated_replies(raw),
            ["第一句", "第二句", "第三句", "第四句", "第五句", "第六句"],
        )

    def test_desktop_click_chooses_only_from_current_pet_replies(self):
        pet = DesktopPet.__new__(DesktopPet)
        pet._pending_reply_job = "job-id"
        pet._playful_replies = ("角色专属甲", "角色专属乙")
        pet._set_local_message = Mock()

        with patch("claw.pet.app.random.choice", return_value="角色专属乙") as choice:
            pet._show_playful_reply()

        choice.assert_called_once_with(pet._playful_replies)
        pet._set_local_message.assert_called_once_with("角色专属乙")
        self.assertIsNone(pet._pending_reply_job)


if __name__ == "__main__":
    unittest.main()
