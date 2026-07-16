param(
    [string]$PythonExe = "",
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

if (-not $PythonExe) {
    $pythonCandidates = @(
        (Join-Path $RepoRoot ".venv-build\Scripts\python.exe"),
        (Join-Path $RepoRoot ".venv\Scripts\python.exe")
    )
    foreach ($candidate in $pythonCandidates) {
        if (Test-Path -LiteralPath $candidate) {
            $PythonExe = $candidate
            break
        }
    }
    if (-not $PythonExe) {
        $PythonExe = "python"
    }
}

Write-Host "==> Building WebUI"
Push-Location (Join-Path $RepoRoot "webui")
npm ci
npm run build
Pop-Location

Write-Host "==> Installing desktop build dependencies"
& $PythonExe -m pip install -e ".[build]"

Write-Host "==> Verifying Tkinter desktop support"
& $PythonExe -c "import tkinter as tk; root = tk.Tk(); root.withdraw(); root.update_idletasks(); root.destroy()"
if ($LASTEXITCODE -ne 0) {
    throw "Tkinter is incomplete in '$PythonExe'. Install Python with Tcl/Tk support or pass -PythonExe pointing to a complete Python installation."
}

Write-Host "==> Building Windows app with PyInstaller"
& $PythonExe -m PyInstaller --noconfirm (Join-Path $RepoRoot "packaging\windows\SJTUClaw.spec")

if (-not $SkipInstaller) {
    $iscc = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($null -eq $iscc) {
        $isccCandidates = @(
            "D:\Inno Setup 7\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
        )
        foreach ($candidate in $isccCandidates) {
            if (Test-Path -LiteralPath $candidate) {
                $iscc = Get-Item -LiteralPath $candidate
                break
            }
        }
    }

    if ($null -eq $iscc) {
        Write-Warning "Inno Setup compiler not found. Install Inno Setup 7/6, add ISCC.exe to PATH, or rerun with -SkipInstaller."
        Write-Host "PyInstaller output: $RepoRoot\dist\SJTUClaw\SJTUClaw.exe"
        exit 0
    }

    Write-Host "==> Building installer with Inno Setup"
    $isccPath = if ($iscc -is [System.Management.Automation.CommandInfo]) {
        $iscc.Source
    } else {
        $iscc.FullName
    }
    & $isccPath (Join-Path $RepoRoot "packaging\windows\SJTUClaw.iss")
    Write-Host "Installer output: $RepoRoot\dist\installer"
}
