param(
    [string]$Name = "NetworkPathDiagnostics"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "python command was not found."
}

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

python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name $Name `
    --paths . `
    @ExcludeArgs `
    app\main.py

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
        if (-not ($Resolved.Path.StartsWith($PySideDir.Path, [System.StringComparison]::OrdinalIgnoreCase))) {
            throw "Refusing to remove outside PySide6 directory: $Resolved"
        }
        Remove-Item -LiteralPath $Resolved.Path -Recurse -Force
    }
}

Write-Host "Build complete: dist\$Name\$Name.exe"
