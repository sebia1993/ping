# MultiPingCheck

Windows 일반 사용자 권한에서 동작하는 Python 기반 다중 대상 ping 모니터링 도구입니다.
상용 제품의 이름, UI, 로고, 디자인 자산을 복제하지 않는 자체 운영 도구로 구현합니다.

## Design Source

Figma: https://www.figma.com/design/r2a3SBQ6f4lLnNOs3neWWb

Figma 파일은 MVP 데스크톱 화면, 상태/개발 핸드오프, 디자인 힌트를 포함합니다.
UI 변경은 이 파일을 우선 기준으로 맞춥니다.

## 주요 기능

- 여러 IPv4 주소 입력
- 등록된 IPv4 대상 전체를 주기적으로 ping 측정
- 대상별 실시간 그래프 행 표시
- 정상/주의/장애 상태 색상 표시
- 대상별 이름 지정, 일시중지, 삭제
- CSV, XLSX, Report, PNG 저장
- 세션 로그 segmented CSV 저장 및 복구
- 중간 Hop ICMP 제한 가능성을 고려한 분석 문장
- ICMP/TCP Connect 프로브 엔진 설정 저장

## 실행

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
python scripts\verify_release.py --live --exe
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

로컬 작업을 commit한 뒤 실행합니다. 이 스크립트는 Windows EXE를 빌드하고, 릴리즈 검증을 실행하고,
`release\` 아래 ZIP 패키지를 만든 뒤 Git tag와 GitHub Release asset을 생성합니다.
EXE/ZIP은 Git 저장소에 commit하지 않고 GitHub Release 첨부파일로만 올립니다.

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
- 전체 샘플은 `exports\session_logs`의 세션 로그에 segmented CSV로 저장되어 CSV/XLSX 내보내기에 사용됩니다.
- CSV/XLSX/Report 저장은 백그라운드 작업으로 실행되어 UI 응답성을 유지합니다.
- IPv4 대상은 기본 최대 50개까지 측정하며, 초과 입력 시 처음 50개 사용 여부를 확인합니다.
- 선택한 IPv4 대상 Ping은 즉시 시작하고, Tracert 결과는 완료되는 대로 Hop 테이블에 반영합니다.
- 대상 그룹 JSON에는 이름, 생성 시각, 대상 출처, 대상별 측정 설정 요약, 대상별 주기 override가 포함됩니다.
- 알림 프리셋 JSON에는 이름, 생성 시각, 활성 조건/액션 요약이 포함됩니다.
- Session Manager와 세션 ZIP manifest는 Mode, Engine, TCP Port를 분리해서 기록합니다.
- 저장된 세션에서 Resume 후 Start하면 새 세션에 원본 세션 ID가 남아 측정 이력을 추적할 수 있습니다.
- 세션 로그 저장 오류가 발생하면 세션을 `Pause` 상태와 원인 코드로 남깁니다.
- Session Manager 새로고침은 기존 세션 CSV segment를 다시 읽어 샘플 수, 마지막 시각, 대상 수, segment 목록을 보정합니다.
- EXE 빌드는 사용하지 않는 대형 모듈을 제외해 배포 크기를 줄입니다.

## 주의

중간 Hop의 packet loss는 실제 장애가 아니라 ICMP rate limit 또는 방화벽 정책일 수 있습니다.
이 도구의 분석 결과는 확정 진단이 아니라 장애 가능성 판단을 돕기 위한 참고 정보입니다.
