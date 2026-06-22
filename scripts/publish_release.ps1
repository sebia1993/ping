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

# 이 스크립트는 로컬에서 검증된 Windows 실행 파일을 만든 뒤 ZIP으로 묶고,
# 선택적으로 GitHub Release까지 올리는 배포 자동화입니다.
# 평소 백업/동기화만 할 때는 Git commit/push만 쓰고, 실제 첨부 ZIP Release가 필요할 때 실행합니다.

function Require-Command {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [Parameter(Mandatory = $true)][string]$InstallHint
    )

    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        throw "$Command command was not found. $InstallHint"
    }
}

function Resolve-CommandPath {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$FallbackPaths = @()
    )

    $Found = Get-Command $Command -ErrorAction SilentlyContinue
    if ($Found) {
        return $Found.Source
    }
    # GitHub CLI처럼 설치되어 있어도 PATH에 없는 도구는 흔한 설치 위치를 추가로 확인합니다.
    foreach ($Path in $FallbackPaths) {
        if (Test-Path -LiteralPath $Path) {
            return $Path
        }
    }
    return ""
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

# 배포에 필요한 기본 도구가 있는지 먼저 확인합니다.
Require-Command "git" "Install Git for Windows and retry."
Require-Command "python" "Install Python and retry."
$GhCommand = Resolve-CommandPath "gh" @(
    "C:\Program Files\GitHub CLI\gh.exe",
    "C:\Program Files (x86)\GitHub CLI\gh.exe",
    "$env:LOCALAPPDATA\Programs\GitHub CLI\gh.exe"
)
if (-not $SkipUpload) {
    # GitHub Release 업로드는 GitHub CLI 로그인 상태가 필요합니다.
    # 로그인하지 않은 PC에서는 -SkipUpload로 로컬 ZIP 생성까지만 수행할 수 있습니다.
    if (-not $GhCommand) {
        throw "gh command was not found. Install GitHub CLI from https://cli.github.com/ and run: gh auth login"
    }
}

# 브랜치와 origin remote가 없으면 GitHub에 어떤 위치로 올릴지 알 수 없으므로 중단합니다.
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
    # 소스 변경사항이 섞인 상태에서 Release를 만들면 어떤 코드가 배포됐는지 추적하기 어렵습니다.
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

$PreviousTag = (& git describe --tags --abbrev=0 2>$null).Trim()
if ($LASTEXITCODE -ne 0) {
    $PreviousTag = ""
}
$ChangeRange = if ($PreviousTag) { "$PreviousTag..HEAD" } else { "HEAD" }
$ChangeLines = @(& git log --pretty=format:"- %s (%h)" --no-merges $ChangeRange)
if ($LASTEXITCODE -ne 0 -or -not $ChangeLines) {
    $ChangeLines = @("- 변경 커밋을 자동으로 찾지 못했습니다. GitHub의 커밋 목록을 확인하세요.")
}
$ChangeSummaryText = ($ChangeLines -join [Environment]::NewLine)

if (-not $SkipUpload) {
    $AuthOutput = (& $GhCommand auth status 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "GitHub CLI is not authenticated. Run: gh auth login"
    }
    Invoke-Checked "git" @("push", "origin", $Branch)
}

if (-not $SkipBuild) {
    # PyInstaller 빌드 스크립트가 dist\<Name>\<Name>.exe를 생성합니다.
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
    # 실행 파일이 실제로 뜨는지, 필수 파일이 빠지지 않았는지 검증합니다.
    Invoke-Checked "python" @("scripts\verify_release.py", "--exe")
}

# 검증된 dist 폴더 전체를 ZIP으로 묶습니다. 사용자는 이 ZIP을 받아 압축을 풀고 EXE를 실행하면 됩니다.
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

# 여기부터는 GitHub Release에 ZIP 파일을 첨부하는 단계입니다.
& git rev-parse -q --verify "refs/tags/$Tag" *> $null
if ($LASTEXITCODE -eq 0) {
    throw "Git tag already exists: $Tag"
}

Invoke-Checked "git" @("tag", "-a", $Tag, "-m", $Title)
Invoke-Checked "git" @("push", "origin", $Tag)

$NotesPath = Join-Path $env:TEMP "${Name}_${SafeTag}_release_notes.md"
@"
## 변경사항

$ChangeSummaryText

## 실행 파일

- 실행 파일: $($ExeItem.Name) ($($ExeItem.Length) bytes)
- 압축 파일: $($ZipItem.Name) ($($ZipItem.Length) bytes)

## 검증

- 검증 명령: scripts\verify_release.py --exe
- 기준 커밋: $Head
- 브랜치: $Branch
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

Invoke-Checked $GhCommand $ReleaseArgs

Write-Host "GitHub Release published: $Tag"
Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
