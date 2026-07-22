{% if system == 'Windows' %}
## 平台策略（Windows）

- 当前运行环境是 Windows。不要默认 `grep`、`sed`、`awk` 等 GNU 工具可用。
- 优先使用专用文件工具或 Windows 原生命令，并采用与 PowerShell 兼容的语法。
- 如果终端输出出现乱码，请优先切换为 UTF-8 编码后重试。
{% else %}
## 平台策略（POSIX）

- 当前运行环境是 POSIX 系统。优先使用 UTF-8 编码和标准 shell 工具。
- 当专用文件工具更简单或更可靠时，优先使用专用工具，而非 shell 命令。
{% endif %}
