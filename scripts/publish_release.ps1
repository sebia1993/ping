param(
    [string]$Name = "MultiPingCheck",
    [string]$Tag = "",
    [string]$Title = "",
    [string]$Notes = "",
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

function Get-KstNow {
    try {
        $Kst = [System.TimeZoneInfo]::FindSystemTimeZoneById("Korea Standard Time")
        return [System.TimeZoneInfo]::ConvertTimeFromUtc([System.DateTime]::UtcNow, $Kst)
    }
    catch {
        return [System.DateTime]::UtcNow.AddHours(9)
    }
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
    # 태그를 직접 지정하지 않으면 KST 현재 시각으로 새 버전명을 만듭니다.
    # 같은 날 여러 번 배포해도 겹치지 않도록 시분초까지 포함합니다.
    $Tag = "v$((Get-KstNow).ToString('yyyy.MM.dd-HHmmss'))"
}
if (-not $Title) {
    $Title = "$Name $Tag"
}

$Head = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or -not $Head) {
    throw "Could not determine current commit."
}

$PreviousTag = (& git describe --tags --abbrev=0 2>$null).Trim()
if ($LASTEXITCODE -ne 0) {
    $PreviousTag = ""
}
$ChangeRange = if ($PreviousTag) { "$PreviousTag..HEAD" } else { "HEAD" }
# 릴리즈 노트의 변경사항은 직전 태그 이후 commit 제목으로 자동 작성합니다.
# 사용자가 GitHub Release에서 어떤 작업이 들어갔는지 바로 볼 수 있게 하기 위한 단계입니다.
$ChangeLines = @(& git log --pretty=format:"- %s (%h)" --no-merges $ChangeRange)
if ($LASTEXITCODE -ne 0 -or -not $ChangeLines) {
    $ChangeLines = @("- 변경 커밋을 자동으로 찾지 못했습니다. GitHub의 커밋 목록을 확인하세요.")
}
$ChangeSummaryText = ($ChangeLines -join [Environment]::NewLine)

if (-not $SkipUpload) {
    $HasGhToken = -not [string]::IsNullOrWhiteSpace($env:GH_TOKEN) -or -not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)
    if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN) -and -not [string]::IsNullOrWhiteSpace($env:GITHUB_TOKEN)) {
        $env:GH_TOKEN = $env:GITHUB_TOKEN
    }
    if (-not $HasGhToken) {
        $AuthOutput = (& $GhCommand auth status 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw "GitHub CLI is not authenticated. Run: gh auth login"
        }
    }
    else {
        Write-Host "Using GitHub token from environment for release upload."
    }
    Invoke-Checked "git" @("push", "origin", $Branch)
}

if (-not $SkipVerify) {
    # 소스 검증을 먼저 실행해 빌드 전에 빠르게 실패시키고, --exe 단계의 중복 pytest를 피합니다.
    Invoke-Checked "python" @("scripts\verify_release.py")
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

$ExeItem = Get-Item -LiteralPath $ExePath.Path
$PackageReadmePath = Join-Path $DistDir.Path "README-실행안내.txt"
@"
MultiPingCheck 실행 안내

1. ZIP 파일을 먼저 원하는 폴더에 압축 해제합니다.
2. 압축 해제한 폴더 안의 $($ExeItem.Name)을 실행합니다.
3. Python, PySide6, GitHub CLI는 사용자 PC에 따로 설치할 필요가 없습니다.
4. Windows SmartScreen 또는 Defender 경고가 처음 실행 시 표시될 수 있습니다. 배포 출처와 SHA256 값을 확인한 뒤 실행 여부를 결정하세요.

배포 정보

- 프로그램: $Name
- 태그: $Tag
- 기준 커밋: $Head
- 브랜치: $Branch
"@ | Set-Content -LiteralPath $PackageReadmePath -Encoding UTF8

$SafeTag = Get-SafeFileToken $Tag
$ZipPath = Join-Path $ReleaseDir "${Name}_${SafeTag}.zip"
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path (Join-Path $DistDir.Path "*") -DestinationPath $ZipPath -Force

$ZipItem = Get-Item -LiteralPath $ZipPath
$ZipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipItem.FullName).Hash.ToLowerInvariant()
$ChecksumPath = "$($ZipItem.FullName).sha256"
Set-Content -LiteralPath $ChecksumPath -Value "$ZipHash  $($ZipItem.Name)" -Encoding ASCII
$ChecksumItem = Get-Item -LiteralPath $ChecksumPath

if ($SkipUpload) {
    Write-Host "Release package created without upload."
    Write-Host "EXE: $($ExeItem.FullName) ($($ExeItem.Length) bytes)"
    Write-Host "ZIP: $($ZipItem.FullName) ($($ZipItem.Length) bytes)"
    Write-Host "SHA256: $ZipHash"
    Write-Host "CHECKSUM: $($ChecksumItem.FullName)"
    exit 0
}

# 여기부터는 GitHub Release에 ZIP 파일을 첨부하는 단계입니다.
# Git tag는 "이 ZIP이 어떤 소스 커밋에서 나왔는지" 표시하는 기준점입니다.
# 나중에 문제가 생기면 태그를 보고 같은 소스 상태를 다시 찾을 수 있습니다.
& git rev-parse -q --verify "refs/tags/$Tag" *> $null
if ($LASTEXITCODE -eq 0) {
    throw "Git tag already exists: $Tag"
}

Invoke-Checked "git" @("tag", "-a", $Tag, "-m", $Title)
Invoke-Checked "git" @("push", "origin", $Tag)

$NotesPath = Join-Path $env:TEMP "${Name}_${SafeTag}_release_notes.md"
$NotesText = $Notes.Trim()
$OptionalNotesSection = if ($NotesText) {
    @"
## 릴리즈 메모

$NotesText

"@
}
else {
    ""
}

@"
$OptionalNotesSection
## 변경사항

$ChangeSummaryText

## 실행 파일

- 실행 파일: $($ExeItem.Name) ($($ExeItem.Length) bytes)
- 압축 파일: $($ZipItem.Name) ($($ZipItem.Length) bytes)
- 압축 해제 안내: README-실행안내.txt
- ZIP SHA256: $ZipHash

## 검증

- 검증 명령: scripts\verify_release.py --exe
- 기준 커밋 SHA: $Head
- 브랜치: $Branch
"@ | Set-Content -LiteralPath $NotesPath -Encoding UTF8

$ReleaseArgs = @(
    "release",
    "create",
    $Tag,
    $ZipItem.FullName,
    $ChecksumItem.FullName,
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
Write-Host "SHA256: $ZipHash"
Write-Host "CHECKSUM: $($ChecksumItem.FullName)"
