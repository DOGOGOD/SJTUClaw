"""Prompt template engine — variable interpolation.

Supports ``{{ variable }}`` substitution and lightweight Jinja-style
``{% if variable %}`` / ``{% if variable == 'value' %}`` conditional blocks,
with optional ``{% else %}`` branches, in its agent templates (identity,
platform_policy, tool_contract, etc.).

No external dependencies — this is a minimal self-contained renderer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render(template: str, variables: Mapping[str, Any] | None = None) -> str:
    """Render a template string with ``{{ var }}`` substitution and
    lightweight ``{% if ... %}...{% else %}...{% endif %}`` blocks.

    Args:
        template: the template string.
        variables: dict of variable name → value. Values are coerced to
            ``str`` for substitution and tested for truthiness in ``if``.

    Returns:
        The rendered string.
    """
    vars_dict: dict[str, Any] = dict(variables or {})
    result = template

    # 1. Handle {% if var %}...{% endif %} blocks (non-nested)
    result = _expand_conditionals(result, vars_dict)

    # 2. Handle {{ var }} substitutions
    result = _expand_variables(result, vars_dict)

    # 3. Clean up: remove trailing whitespace on lines that are now empty
    result = re.sub(r"(?m)^\s+$", "", result)

    # 4. Collapse 3+ blank lines into 2
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result


def render_file(path: Path, variables: Mapping[str, Any] | None = None) -> str:
    """Load and render a template file."""
    content = path.read_text(encoding="utf-8")
    return render(content, variables)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

_IF_BLOCK_RE = re.compile(
    r"\{%\s*if\s+(\w[\w.-]*)(?:\s*==\s*(['\"])(.*?)\2)?\s*%\}"
    r"(.*?)(?:\{%\s*else\s*%\}(.*?))?\{%\s*endif\s*%\}",
    re.DOTALL,
)
_VAR_RE = re.compile(r"\{\{\s*(\w[\w.-]*)\s*\}\}")


def _truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (int, float)):
        return bool(value)
    return True


def _expand_conditionals(text: str, variables: dict[str, Any]) -> str:
    """Expand supported conditional blocks, including optional else branches."""
    def _replace(m: re.Match) -> str:
        var_name = m.group(1)
        expected = m.group(3)
        truthy_body = m.group(4)
        fallback_body = m.group(5) or ""
        value = variables.get(var_name)
        condition_matches = (
            str(value) == expected if expected is not None else _truthy(value)
        )
        return truthy_body if condition_matches else fallback_body
    return _IF_BLOCK_RE.sub(_replace, text)


def _expand_variables(text: str, variables: dict[str, Any]) -> str:
    """Expand ``{{ var }}`` placeholders."""
    def _replace(m: re.Match) -> str:
        var_name = m.group(1)
        value = variables.get(var_name)
        return str(value) if value is not None else f"{{{{{var_name}}}}}"
    return _VAR_RE.sub(_replace, text)
