<#
.SYNOPSIS
  Builds AndroidLite.exe with PyInstaller and packages it as an MSIX.

.DESCRIPTION
  Build-time needs only: Python 3.10+, PyInstaller (installed automatically) and
  the Windows 10/11 SDK (MakeAppx.exe, SignTool.exe). Runtime needs nothing extra
  beyond the app itself, plus QEMU, which is either bundled with -QemuDir or
  installed once on first run.

  Unsigned MSIX files cannot be installed. Use -PfxPath to sign with a
  certificate whose subject matches -Publisher, or submit to Partner Center
  and let the Store sign the package.

.EXAMPLE
  .\packaging\build_msix.ps1 -Version 1.2.0.0 -Publisher "CN=Contoso" -QemuDir C:\qemu-portable
#>
param(
    [string]$Version = "1.2.0.0",
    [string]$Publisher = "CN=Developer",
    [string]$QemuDir = "",          # optional: portable QEMU folder to bundle (exe + DLLs)
    [string]$PfxPath = "",          # optional: .pfx code-signing certificate
    [string]$PfxPassword = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    # 1. Single-file exe (GUI, no console window)
    python -m PyInstaller --noconfirm --onefile --windowed --name AndroidLite `
        --icon packaging\AndroidLite.ico main.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    # 2. Stage the package contents
    $stage = Join-Path $root "build\msix"
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stage | Out-Null
    Copy-Item (Join-Path $root "dist\AndroidLite.exe") $stage
    Copy-Item (Join-Path $root "packaging\msix\Assets") -Destination $stage -Recurse
    if ($QemuDir) {
        Copy-Item (Join-Path $QemuDir "*") -Destination $stage -Recurse -Force
    }

    # 3. Stamp version and publisher into the manifest (XML-safe edit)
    [xml]$manifest = Get-Content (Join-Path $root "packaging\msix\AppxManifest.xml") -Raw
    $manifest.Package.Identity.Version = $Version
    $manifest.Package.Identity.Publisher = $Publisher
    $manifest.Package.Properties.PublisherDisplayName = ($Publisher -replace '^CN=', '')
    $manifest.Save((Join-Path $stage "AppxManifest.xml"))

    # 4. Pack
    $makeappx = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\makeappx.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $makeappx) { throw "MakeAppx.exe not found. Install the Windows 10/11 SDK." }
    New-Item -ItemType Directory -Path (Join-Path $root "dist") -Force | Out-Null
    $out = Join-Path $root "dist\AndroidLite_$Version.msix"
    & $makeappx.FullName pack /d $stage /p $out /o
    if ($LASTEXITCODE -ne 0) { throw "MakeAppx failed." }

    # 5. Optional signing
    if ($PfxPath) {
        $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" `
            -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
        if (-not $signtool) { throw "SignTool.exe not found. Install the Windows SDK." }
        & $signtool.FullName sign /fd SHA256 /f $PfxPath /p $PfxPassword $out
        if ($LASTEXITCODE -ne 0) { throw "Signing failed." }
    } else {
        Write-Warning "Package is unsigned. It will not install until it is signed or submitted to the Store."
    }
    Write-Host "Built $out"
}
finally {
    Pop-Location
}
