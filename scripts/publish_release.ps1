param(
    [string]$Name = "NetworkPathDiagnostics",
    [string]$Tag = "",
    [string]$Title = "",
    [switch]$SkipBuild,
    [switch]$SkipVerify,
    [switch]$SkipUpload,
    [switch]$Draft,
    [switch]$Prerelease,
    [switch]$AllowDirty
)

$ErrorActionPreference = "Stop"

function Require-Command {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$InstallHint
    )

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command command was not found. $InstallHint"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $FilePath $($Arguments -join ' ')"
    }
}

function Get-SafeFileToken {
    param([Parameter(Mandatory = $true)][string]$Value)
    return ($Value -replace '[^A-Za-z0-9._-]', '_')
}

$Root = Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")
Set-Location -LiteralPath $Root.Path

Require-Command "git" "Install Git for Windows and retry."
Require-Command "python" "Install Python and retry."
if (-not $SkipUpload) {
    Require-Command "gh" "Install GitHub CLI from https://cli.github.com/ and run: gh auth login"
}

$Branch = (& git rev-parse --abbrev-ref HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Branch -or $Branch -eq "HEAD") {
    throw "Release publishing requires a normal checked-out branch."
}

$Remote = (& git remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Remote) {
    throw "Git remote 'origin' was not found."
}

$Status = (& git status --porcelain)
if ($Status -and -not $AllowDirty) {
    throw "Working tree has uncommitted changes. Commit local work before publishing, or pass -AllowDirty for packaging-only checks."
}

if (-not $Tag) {
    $Tag = "v$(Get-Date -Format 'yyyy.MM.dd-HHmmss')"
}
if (-not $Title) {
    $Title = "$Name $Tag"
}

$Head = (& git rev-parse --short HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Head) {
    throw "Could not determine current commit."
}

if (-not $SkipUpload) {
    $AuthOutput = (& gh auth status 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI is not authenticated. Run: gh auth login"
    }
    Invoke-Checked "git" @("push", "origin", $Branch)
}

if (-not $SkipBuild) {
    Invoke-Checked "powershell" @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        (Join-Path $Root.Path "build_windows_exe.ps1"),
        "-Name",
        $Name
    )
}

if (-not $SkipVerify) {
    Invoke-Checked "python" @("scripts\verify_release.py", "--exe")
}

$DistDir = Resolve-Path -LiteralPath (Join-Path $Root.Path "dist\$Name")
$ExePath = Resolve-Path -LiteralPath (Join-Path $DistDir.Path "$Name.exe")
$ReleaseDir = Join-Path $Root.Path "release"
New-Item -ItemType Directory -Force -Path $ReleaseDir | Out-Null

$SafeTag = Get-SafeFileToken $Tag
$ZipPath = Join-Path $ReleaseDir "${Name}_${SafeTag}.zip"
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $DistDir.Path "*") -DestinationPath $ZipPath -Force

$ZipItem = Get-Item -LiteralPath $ZipPath
$ExeItem = Get-Item -LiteralPath $ExePath.Path

if ($SkipUpload) {
    Write-Host "Release package created without upload."
    Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
    Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
    exit 0
}

& git rev-parse -q --verify "refs/tags/$Tag" *> $null
if ($LASTEXITCODE -eq 0) {
    throw "Git tag already exists: $Tag"
}

Invoke-Checked "git" @("tag", "-a", $Tag, "-m", $Title)
Invoke-Checked "git" @("push", "origin", $Tag)

$NotesPath = Join-Path $env:TEMP "${Name}_${SafeTag}_release_notes.md"
@"
Automated local release package.

- Commit: $Head
- Branch: $Branch
- EXE: $($ExeItem.Name) ($($ExeItem.Length) bytes)
- ZIP: $($ZipItem.Name) ($($ZipItem.Length) bytes)
- Verification: scripts\verify_release.py --exe
"@ | Set-Content -LiteralPath $NotesPath -Encoding UTF8

$ReleaseArgs = @(
    "release",
    "create",
    $Tag,
    $ZipItem.FullName,
    "--title",
    $Title,
    "--notes-file",
    $NotesPath
)
if ($Draft) {
    $ReleaseArgs += "--draft"
}
if ($Prerelease) {
    $ReleaseArgs += "--prerelease"
}

Invoke-Checked "gh" $ReleaseArgs

Write-Host "GitHub Release published: $Tag"
Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
