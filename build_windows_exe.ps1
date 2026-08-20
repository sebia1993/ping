param(
    [string]$Name = "MultiPingCheck"
)

$ErrorActionPreference = "Stop"

# 이 스크립트는 Python 소스 코드를 Windows에서 바로 실행할 수 있는 EXE 폴더로 묶습니다.
# 결과물은 dist\<프로그램이름>\<프로그램이름>.exe에 만들어집니다.
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python command was not found."
}

# PyInstaller는 import 가능한 패키지를 넓게 훑기 때문에, 쓰지 않는 큰 라이브러리까지
# EXE에 들어갈 수 있습니다. 아래 목록은 용량을 줄이기 위해 명시적으로 제외하는 항목입니다.
$ExcludeModules = @(
    "numpy",
    "PIL",
    "lxml",
    "scipy",
    "pandas",
    "matplotlib",
    "bs4",
    "html5lib",
    "yaml",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtNetwork",
    "PySide6.QtOpenGL",
    "PySide6.QtPdf",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickWidgets",
    "PySide6.QtSvg",
    "PySide6.QtVirtualKeyboard",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets"
)

$ExcludeArgs = @()
foreach ($Module in $ExcludeModules) {
    $ExcludeArgs += @("--exclude-module", $Module)
}

# --windowed는 실행할 때 검은 콘솔 창이 뜨지 않게 하는 옵션입니다.
# app\main.py가 실제 GUI 프로그램의 시작점입니다.
$BuildInfoTempDir = Join-Path ([System.IO.Path]::GetTempPath()) "multipingcheck-build-info-$PID-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $BuildInfoTempDir | Out-Null
$BuildInfoPath = Join-Path $BuildInfoTempDir "multipingcheck_build_info.json"
try {
    # Git과 소스가 없는 사내 PC에서도 테스트한 빌드를 식별할 수 있도록 JSON을 EXE에 포함합니다.
    python scripts\generate_build_info.py `
        --output $BuildInfoPath `
        --program-name $Name `
        --distribution "Windows Portable EXE"
    if ($LASTEXITCODE -ne 0) {
        throw "Build metadata generation failed."
    }

    python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name $Name `
        --paths . `
        --add-data "$BuildInfoPath;." `
        @ExcludeArgs `
        app\main.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }
}
finally {
    if (Test-Path -LiteralPath $BuildInfoTempDir) {
        $ResolvedBuildInfoTempDir = Resolve-Path -LiteralPath $BuildInfoTempDir
        $ResolvedSystemTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        if (-not $ResolvedBuildInfoTempDir.Path.StartsWith($ResolvedSystemTemp, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove build metadata outside the system temporary directory: $ResolvedBuildInfoTempDir"
        }
        Remove-Item -LiteralPath $ResolvedBuildInfoTempDir.Path -Recurse -Force
    }
}

# PySide6에는 이 프로그램이 쓰지 않는 플러그인과 DLL이 많이 포함됩니다.
# 빌드 후 dist 폴더 안에서만 불필요한 파일을 지워 ZIP 크기를 줄입니다.
$PySideDir = Resolve-Path -LiteralPath "dist\$Name\_internal\PySide6"
$CleanupItems = @(
    "translations",
    "opengl32sw.dll",
    "Qt6Quick.dll",
    "Qt6Qml.dll",
    "Qt6QmlModels.dll",
    "Qt6QmlMeta.dll",
    "Qt6QmlWorkerScript.dll",
    "Qt6Pdf.dll",
    "Qt6OpenGL.dll",
    "Qt6Network.dll",
    "Qt6Svg.dll",
    "Qt6VirtualKeyboard.dll",
    "plugins\generic",
    "plugins\iconengines",
    "plugins\imageformats",
    "plugins\platforminputcontexts",
    "plugins\platforms\qdirect2d.dll",
    "plugins\platforms\qminimal.dll",
    "plugins\platforms\qoffscreen.dll"
)

foreach ($Item in $CleanupItems) {
    $Target = Join-Path $PySideDir $Item
    if (Test-Path -LiteralPath $Target) {
        $Resolved = Resolve-Path -LiteralPath $Target
        # 계산된 삭제 경로가 PySide6 폴더 밖으로 나가면 즉시 중단합니다.
        # 경로 실수로 프로젝트나 사용자 파일을 지우지 않기 위한 안전장치입니다.
        if (-not ($Resolved.Path.StartsWith($PySideDir.Path, [System.StringComparison]::OrdinalIgnoreCase))) {
            throw "Refusing to remove outside PySide6 directory: $Resolved"
        }
        Remove-Item -LiteralPath $Resolved.Path -Recurse -Force
    }
}

Write-Host "Build complete: dist\$Name\$Name.exe"
