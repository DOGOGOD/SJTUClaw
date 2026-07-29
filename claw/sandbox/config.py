"""Configuration for the optional microsandbox execution backend."""

from __future__ import annotations

from dataclasses import dataclass

from claw.runtime_settings import setting_value


class SandboxConfigError(ValueError):
    """Raised when sandbox configuration is invalid."""


def _integer(
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw = setting_value(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise SandboxConfigError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise SandboxConfigError(f"{name} 必须大于等于 {minimum}")
    if maximum is not None and value > maximum:
        raise SandboxConfigError(f"{name} 必须小于等于 {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """Effective sandbox settings.

    ``auto`` uses microsandbox when its Python SDK/runtime is installed and
    otherwise preserves the legacy workspace behaviour.  ``required`` is
    fail-closed: native tools never fall back to the host.
    """

    mode: str = "off"
    image: str = "python:3.12-bookworm"
    cpus: int = 2
    memory_mib: int = 2048
    max_duration_s: int = 6 * 60 * 60
    idle_timeout_s: int = 60 * 60
    network: str = "public"
    security: str = "restricted"
    workspace_quota_mib: int = 4096

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


def load_sandbox_config() -> SandboxConfig:
    """Load and validate sandbox settings from runtime settings or the env."""
    mode = setting_value("SANDBOX_MODE", "off").strip().lower() or "off"
    if mode not in {"off", "auto", "required"}:
        raise SandboxConfigError(
            "SANDBOX_MODE 仅支持 off、auto 或 required"
        )

    network = (
        setting_value("SANDBOX_NETWORK", "public").strip().lower() or "public"
    )
    if network not in {"none", "public"}:
        raise SandboxConfigError(
            "SANDBOX_NETWORK 仅支持 none 或 public"
        )

    security = (
        setting_value("SANDBOX_SECURITY", "restricted").strip().lower()
        or "restricted"
    )
    if security not in {"restricted", "default"}:
        raise SandboxConfigError(
            "SANDBOX_SECURITY 仅支持 restricted 或 default"
        )

    image = setting_value("SANDBOX_IMAGE", "python:3.12-bookworm").strip()
    if not image:
        raise SandboxConfigError("SANDBOX_IMAGE 不能为空")

    return SandboxConfig(
        mode=mode,
        image=image,
        cpus=_integer("SANDBOX_CPUS", 2, maximum=255),
        memory_mib=_integer(
            "SANDBOX_MEMORY_MIB", 2048, minimum=256, maximum=1_048_576
        ),
        max_duration_s=_integer("SANDBOX_MAX_DURATION_S", 6 * 60 * 60),
        idle_timeout_s=_integer("SANDBOX_IDLE_TIMEOUT_S", 60 * 60),
        network=network,
        security=security,
        workspace_quota_mib=_integer(
            "SANDBOX_WORKSPACE_QUOTA_MIB", 4096, minimum=64
        ),
    )
