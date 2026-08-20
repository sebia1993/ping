# MultiPingCheck

Windows 일반 사용자 권한에서 동작하는 Python 기반 다중 대상 ping 모니터링 도구입니다.
상용 제품의 이름, UI, 로고, 디자인 자산을 복제하지 않는 자체 운영 도구로 구현합니다.

## Design Source

Figma: https://www.figma.com/design/r2a3SBQ6f4lLnNOs3neWWb

Figma 파일은 MVP 데스크톱 화면, 상태/개발 핸드오프, 디자인 힌트를 포함합니다.
UI 변경은 이 파일을 우선 기준으로 맞춥니다.

## 주요 기능

- 여러 IPv4 주소 입력
- 등록된 IPv4 대상 전체를 1초 간격 ICMP로 측정
- 대상별 실시간 그래프 행 표시
- 정상/주의/장애 상태 색상 표시
- 대상별 이름 지정, 일시중지, 삭제
- 측정 중 IPv4 대상 추가
- 최근 1분/5분/10분/30분/1시간 및 측정 전체 그래프
- 세션 로그 segmented CSV 저장 및 복구

현재 기본 화면은 `IPv4 입력 → 시작 → 대상별 그래프 확인 → 중지` 흐름에 집중합니다.
코드에 보존된 TCP Connect, 전체 경로, Session Manager, CSV/XLSX/보고서 내보내기 같은
고급 기능은 기본 화면에서 접근할 수 없으며 자동 검증과 향후 호환을 위해 유지합니다.

## 일반 사용자 빠른 시작

1. GitHub Releases에서 최신 `MultiPingCheck_<버전>.zip`을 받습니다.
2. ZIP을 원하는 폴더에 완전히 압축 해제합니다.
3. 압축 해제한 폴더의 `MultiPingCheck.exe`를 실행합니다.
4. IPv4 주소를 한 줄에 하나씩 입력하거나 Excel의 IP 열을 그대로 붙여넣습니다.
5. `시작`을 누르고 대상별 그래프의 색상을 확인합니다.
6. 측정을 끝낼 때 `중지`를 누릅니다.

상태 색상은 `초록=정상`, `주황=주의`, `빨강=장애`, `회색=대기 또는 일시중지`입니다.
대상 이름은 각 그래프 행에서 지정할 수 있으며, 측정 중에도 대상을 추가하거나 개별 대상을
일시중지·삭제할 수 있습니다. Windows SmartScreen 경고가 처음 표시되면 배포 출처와
릴리즈에 첨부된 SHA256 값을 먼저 확인하세요.

세션 데이터는 `%LOCALAPPDATA%\MultiPingCheck\session_logs`, 진단 로그는
`%LOCALAPPDATA%\MultiPingCheck\logs\multipingcheck.log`에 저장됩니다.
오류 코드가 표시되면 [오류 코드와 조치 방법](docs/error_codes.md)을 확인합니다.

## F12 테스트 요청 생성기

사내에서 실행 파일을 시험하다 수정할 화면이나 기능을 발견하면 `F12`로 로컬 개발자 모드를 열 수 있습니다.
화면 요소 또는 기능을 선택하고 원하는 변경을 입력하면 테스트한 빌드 정보가 포함된 Codex 요청문을
미리 보고 클립보드에 복사할 수 있습니다. 회사 정보 마스킹은 기본 ON이며 비밀번호와 토큰은 항상 제거됩니다.
개발자 모드는 외부 서버 전송이나 인터넷 연결을 사용하지 않습니다.

자세한 사용법은 [F12 개발자 모드 사용 안내](docs/DEVELOPER_MODE.md)를 참고합니다.

## 개발자 실행

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

## 테스트

```powershell
pytest
```

릴리즈 전 로컬 검증:

```powershell
python scripts\verify_release.py
python scripts\verify_release.py --exe
python scripts\verify_release.py --live
```

사내 게이트웨이 또는 업무 사이트 대상의 현장 검증:

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
```

자세한 현장 체크리스트는 [docs/field_verification.md](docs/field_verification.md)를 참고합니다.

## EXE 패키지

```powershell
.\build_windows_exe.ps1
```

생성 결과는 `dist\MultiPingCheck\MultiPingCheck.exe`에 위치합니다.

## GitHub Release Publish

권장 경로는 GitHub Actions의 `Release Windows ZIP` workflow를 `main` 브랜치에서 수동 실행하는 것입니다.
이 workflow는 Windows EXE를 빌드하고, 릴리즈 검증을 실행하고, `release\` 아래 ZIP 패키지를 만든 뒤
Git tag와 GitHub Release asset을 생성합니다. ZIP과 함께 SHA256 checksum 파일도 업로드합니다.
EXE/ZIP/checksum은 Git 저장소에 commit하지 않고 GitHub Release 첨부파일로만 올립니다.
수동 실행 입력값 `tag`, `title`, `notes`는 선택사항이며, `tag`를 비우면 KST 기준 `vYYYY.MM.DD-HHMMSS`가 자동 생성됩니다.
배포 전후 확인 절차는 [릴리즈 체크리스트](docs/release_checklist.md)를 따릅니다.
로컬 `git push` 또는 GitHub CLI 인증이 막히면 [GitHub 인증 설정](docs/github_auth_setup.md)을 먼저 확인합니다.

로컬 Windows PC에서 같은 과정을 실행해야 할 때는 로컬 작업을 commit한 뒤 아래 스크립트를 실행합니다.

Requirements:

- GitHub CLI: https://cli.github.com/
- One-time login: `gh auth login`

```powershell
.\scripts\publish_release.ps1
```

업로드 없이 로컬 패키지만 확인:

```powershell
.\scripts\publish_release.ps1 -SkipUpload -SkipBuild -SkipVerify -AllowDirty
```

## Stability Soak Profiles

이 검증은 simulated probe를 사용하므로 실제 회사망 접근이 필요 없습니다.

```powershell
# scripts\verify_release.py에서 사용하는 빠른 50-target release smoke
python scripts\soak_test.py --profile release

# 4/8/24시간 및 UI 10/20/50대 장시간 검증 suite
python scripts\run_stability_soak_suite.py --dry-run
python scripts\run_stability_soak_suite.py
python scripts\run_stability_soak_suite.py --resume --run-id <RUN_ID>
python scripts\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>

# validate-only는 duration, UI event gap, CPU, memory, thread, pending ping,
# log queue, session log row 기준을 summary JSON에서 다시 확인합니다.

# 30분 50-target 안정성 검증
python scripts\soak_test.py --profile long

# 4/8/24시간 simulated 장시간 검증
python scripts\soak_test.py --profile long4h
python scripts\soak_test.py --profile long8h
python scripts\soak_test.py --profile long24h

# offscreen MainWindow UI freeze 검증
python scripts\soak_test.py --profile ui10
python scripts\soak_test.py --profile ui20
python scripts\soak_test.py --profile ui50
```

## 운영 개선 사항

- 실시간 측정 중 화면 그래프는 최근 관측치를 중심으로 표시합니다.
- 전체 샘플은 `%LOCALAPPDATA%\MultiPingCheck\session_logs`에 segmented CSV로 저장됩니다.
- 이전 버전이 EXE 폴더의 `exports\session_logs`에 저장한 세션은 새 인덱스에서 자동으로 찾아 계속 열 수 있습니다.
- 진단 로그는 `%LOCALAPPDATA%\MultiPingCheck\logs\multipingcheck.log`에 순환 저장됩니다.
- 사용자가 직접 저장하는 파일의 기본 위치는 `%USERPROFILE%\Documents\MultiPingCheck`입니다.
- CSV/XLSX/Report 저장과 전체 세션 그래프 읽기는 백그라운드에서 실행됩니다.
- IPv4 대상은 기본 최대 50개까지 측정하며, 초과 입력 시 처음 50개 사용 여부를 확인합니다.
- 기본 화면은 ICMP 최종 대상 측정만 실행하며 Tracert와 TCP Connect 설정을 노출하지 않습니다.
- 세션 로그 저장 오류가 발생하면 세션을 `Pause` 상태와 원인 코드로 남깁니다.
- 손상된 세션 행은 부분 복구 사실을 표시하고, 불완전한 CSV/XLSX가 정상 산출물로 저장되지 않게 차단합니다.
- EXE 빌드는 사용하지 않는 대형 모듈을 제외해 배포 크기를 줄입니다.

## 주의

중간 Hop의 packet loss는 실제 장애가 아니라 ICMP rate limit 또는 방화벽 정책일 수 있습니다.
이 도구의 분석 결과는 확정 진단이 아니라 장애 가능성 판단을 돕기 위한 참고 정보입니다.
