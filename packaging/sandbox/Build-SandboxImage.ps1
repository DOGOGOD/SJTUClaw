param(
    [string]$Tag = "sjtuclaw-sandbox:py3.12-bookworm",
    [string]$PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$MsbPath = ""
)

$ErrorActionPreference = "Stop"
$sandboxDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-MsbPath {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }

    $command = Get-Command msb -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $candidate = Join-Path (Split-Path -Parent $python.Source) "Scripts\msb.exe"
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    if ($env:CONDA_PREFIX) {
        $candidate = Join-Path $env:CONDA_PREFIX "Scripts\msb.exe"
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "找不到 msb。请安装 microsandbox，或通过 -MsbPath 指定 msb.exe。"
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $docker) {
    throw "找不到 Docker CLI。请先安装并启动 Docker Desktop。"
}

& $docker.Source info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker daemon 未运行。请先启动 Docker Desktop。"
}

$resolvedMsb = Resolve-MsbPath -ExplicitPath $MsbPath
$archive = Join-Path (
    [IO.Path]::GetTempPath()
) ("sjtuclaw-sandbox-" + [guid]::NewGuid().ToString("N") + ".tar")

try {
    Write-Host "构建基础镜像 $Tag ..."
    & $docker.Source build `
        --build-arg "PIP_INDEX_URL=$PipIndexUrl" `
        --tag $Tag `
        --file (Join-Path $sandboxDir "Dockerfile") `
        $sandboxDir
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 镜像构建失败。"
    }

    Write-Host "导出镜像归档 ..."
    & $docker.Source save --output $archive $Tag
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 镜像导出失败。"
    }

    Write-Host "导入 microsandbox 镜像缓存 ..."
    & $resolvedMsb load --input $archive --tag $Tag
    if ($LASTEXITCODE -ne 0) {
        throw "microsandbox 镜像导入失败。"
    }

    # On Windows, msb may return after handing the archive path to its
    # background service. Keep the file alive until the cache can resolve the
    # imported tag; otherwise finally could delete it while it is still read.
    $imported = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            $imageJson = (
                & $resolvedMsb image list --format json 2> $null
            ) | Out-String
            $images = $imageJson | ConvertFrom-Json -ErrorAction Stop
            if (@($images | Where-Object { $_.reference -eq $Tag }).Count) {
                $imported = $true
                break
            }
        }
        catch {
            # The cache may be briefly unavailable while the import process
            # commits its database transaction. Retry until the deadline.
        }
        Start-Sleep -Seconds 2
    }
    if (-not $imported) {
        throw "microsandbox 在 120 秒内未确认镜像导入完成。"
    }

    Write-Host ""
    Write-Host "镜像已就绪。请设置并重启 SJTUClaw："
    Write-Host "SANDBOX_IMAGE=$Tag"
}
finally {
    if (Test-Path -LiteralPath $archive) {
        Remove-Item -LiteralPath $archive -Force
    }
}
