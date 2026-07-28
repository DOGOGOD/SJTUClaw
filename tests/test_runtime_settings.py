from __future__ import annotations


def test_concurrent_runtime_updates_preserve_every_key(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from claw import runtime_settings

    monkeypatch.setattr(runtime_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(runtime_settings, "SETTINGS_PATH", tmp_path / "runtime_settings.json")
    monkeypatch.setattr(runtime_settings, "KEY_PATH", tmp_path / "runtime_settings.key")

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(runtime_settings.update_runtime_settings, {f"KEY_{index}": index})
            for index in range(40)
        ]
        for future in futures:
            future.result()

    loaded = runtime_settings.load_runtime_settings()
    assert {key: value for key, value in loaded.items() if key.startswith("KEY_")} == {
        f"KEY_{index}": str(index)
        for index in range(40)
    }


def test_empty_runtime_value_overrides_environment(tmp_path, monkeypatch):
    from claw import runtime_settings

    monkeypatch.setattr(runtime_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(runtime_settings, "SETTINGS_PATH", tmp_path / "runtime_settings.json")
    monkeypatch.setattr(runtime_settings, "KEY_PATH", tmp_path / "runtime_settings.key")
    monkeypatch.setenv("LLM_MODEL", "env-model")

    runtime_settings.update_runtime_settings({"LLM_MODEL": ""})

    assert runtime_settings.setting_value("LLM_MODEL", "fallback") == ""


def test_runtime_settings_raw_roundtrip(tmp_path, monkeypatch):
    from claw import runtime_settings

    monkeypatch.setattr(runtime_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(runtime_settings, "SETTINGS_PATH", tmp_path / "runtime_settings.json")
    monkeypatch.setattr(runtime_settings, "KEY_PATH", tmp_path / "runtime_settings.key")

    payload = {"LLM_BASE_URL": "https://example.test/v1"}
    runtime_settings.replace_runtime_settings_raw(payload)

    assert runtime_settings.load_runtime_settings_raw() == payload


def test_ui_avatar_settings_persist_in_runtime_settings(tmp_path, monkeypatch):
    from claw import runtime_settings

    monkeypatch.setattr(runtime_settings, "SETTINGS_DIR", tmp_path)
    monkeypatch.setattr(runtime_settings, "SETTINGS_PATH", tmp_path / "runtime_settings.json")
    monkeypatch.setattr(runtime_settings, "KEY_PATH", tmp_path / "runtime_settings.key")

    runtime_settings.update_runtime_settings({
        "UI_USER_AVATAR": "custom",
        "UI_USER_AVATAR_IMAGE": "data:image/webp;base64,abc",
    })

    loaded = runtime_settings.load_runtime_settings()
    assert loaded["UI_USER_AVATAR"] == "custom"
    assert loaded["UI_USER_AVATAR_IMAGE"] == "data:image/webp;base64,abc"
