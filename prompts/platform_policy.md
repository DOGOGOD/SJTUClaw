{% if system == 'Windows' %}
## 平台策略 (Windows)
- 你在 Windows 上运行。不要假设 GNU 工具（如 `grep`、`sed`、`awk`）存在。
- 优先使用 Windows 原生命令或文件工具。
- 如果终端输出乱码，尝试启用 UTF-8 编码。
{% else %}
## 平台策略 (POSIX)
- 你在 POSIX 系统上运行。优先使用 UTF-8 和标准 shell 工具。
- 当文件工具更简单或更可靠时，优先使用文件工具而非 shell 命令。
{% endif %}
