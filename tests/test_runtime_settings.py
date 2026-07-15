from __future__ import annotations


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
