# 开发命令速查

```powershell
# 后端
python -m pytest tests/ -v
python -m ruff check claw tests

# Web UI
cd webui
npx vitest run
npm run build

# Windows 发布
.\packaging\windows\build.ps1

# Sandbox 镜像
.\packaging\sandbox\Build-SandboxImage.ps1
```

完整说明见 `README.md`、`docs/testing.md` 和 `docs/windows-packaging.md`。
