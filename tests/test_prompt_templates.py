from pathlib import Path

from claw.prompts.templates import render, render_file


def test_render_supports_truthy_condition_with_else() -> None:
    template = "{% if enabled %}on{% else %}off{% endif %}"

    assert render(template, {"enabled": True}) == "on"
    assert render(template, {"enabled": False}) == "off"


def test_render_supports_quoted_equality_condition() -> None:
    template = "{% if channel == 'cli' %}terminal{% else %}web{% endif %}"

    assert render(template, {"channel": "cli"}) == "terminal"
    assert render(template, {"channel": "web"}) == "web"


def test_bundled_prompt_conditions_render_without_template_syntax() -> None:
    prompt_dir = Path(__file__).resolve().parents[1] / "prompts"
    windows_policy = render_file(
        prompt_dir / "platform_policy.md", {"system": "Windows"}
    )
    posix_policy = render_file(
        prompt_dir / "platform_policy.md", {"system": "Linux"}
    )
    cli_identity = render_file(
        prompt_dir / "identity.md",
        {
            "runtime": "Windows x86_64",
            "workspace_path": "C:/workspace",
            "platform_policy": windows_policy,
            "channel": "cli",
        },
    )

    assert "平台策略（Windows）" in windows_policy
    assert "平台策略（POSIX）" not in windows_policy
    assert "平台策略（POSIX）" in posix_policy
    assert "平台策略（Windows）" not in posix_policy
    assert "## 输出格式" in cli_identity
    assert "{%" not in cli_identity
    assert "{{" not in cli_identity
