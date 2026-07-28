from pathlib import Path

from PIL import Image

from claw.pet.catalog import PetCatalog


def test_xiaohuang_is_a_valid_bundled_pet(tmp_path: Path):
    catalog = PetCatalog(tmp_path / "data")

    pet = catalog.get_pet("xiaohuang")

    assert pet is not None
    assert pet["displayName"] == "线条小狗"
    assert pet["description"] == "一只活力满满、陪你整理思路的黄色线条小狗。"
    assert pet["spriteVersionNumber"] == 1
    assert pet["source"] == "bundled"
    assert pet["readOnly"] is True
    with Image.open(pet["spritesheetPath"]) as spritesheet:
        assert spritesheet.size == (1536, 1872)
        assert spritesheet.mode == "RGBA"


def test_butter_bear_is_a_valid_v2_bundled_pet(tmp_path: Path):
    catalog = PetCatalog(tmp_path / "data")

    pet = catalog.get_pet("butter-bear")

    assert pet is not None
    assert pet["displayName"] == "黄油小熊"
    assert pet["description"] == "一只软乎乎、充满好奇心，喜欢轻快小跑陪你完成任务的黄油色毛绒小熊。"
    assert pet["spriteVersionNumber"] == 2
    assert pet["source"] == "bundled"
    assert pet["readOnly"] is True
    with Image.open(pet["spritesheetPath"]) as spritesheet:
        assert spritesheet.size == (1536, 2288)
        assert spritesheet.mode == "RGBA"
