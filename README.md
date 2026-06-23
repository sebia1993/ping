# MultiPingCheck

Windows 일반 사용자 권한에서 동작하는 Python 기반 네트워크 경로 품질 진단 도구입니다.
상용 제품의 이름, UI, 로고, 디자인, 자산을 복제하지 않는 자체 운영 도구로 구현합니다.

## Design Source

Figma: https://www.figma.com/design/r2a3SBQ6f4lLnNOs3neWWb

Figma 파일은 MVP 데스크톱 화면, 상태/개발 핸드오프, 디자인 노트를 포함합니다.
UI 변경 시 이 파일을 우선 기준으로 맞춥니다.

## 주요 기능

- 여러 IPv4 주소 입력
- 선택한 IPv4 1개에 대한 Windows `tracert` 기반 최초 경로 탐색
- 등록된 모든 IPv4와 경로 Hop에 대한 주기적 `ping` 측정
- Hop별 현재/평균/최소/최대 지연시간, 손실률, 최근 손실률, Timeout, Jitter 표시
- 최종 대상 latency 그래프
- 그래프 확대 별도 창
- CSV 저장, XLSX 저장, 장애 분석 요약 리포트 저장
- 중간 Hop ICMP 제한 가능성을 고려한 가능성 기반 분석 문장
- 세션별 ICMP/TCP Connect 프로브 엔진과 TCP 포트 저장/복구

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

릴리스 전 로컬 검증:

```powershell
python scripts\verify_release.py
python scripts\verify_release.py --live --exe
```

사내 게이트웨이 또는 업무 사이트 대상의 현장 검증:

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
```

자세한 현장 체크리스트는 [docs/field_verification.md](docs/field_verification.md)를 참고합니다.

## EXE 패키징

```powershell
.\build_windows_exe.ps1
```

생성 결과는 `dist\MultiPingCheck\MultiPingCheck.exe`에 위치합니다.

## GitHub Release publish

Use this after local work has been committed. The script builds the Windows EXE,
runs release verification, creates a ZIP package under `release\`, creates a Git
tag, and uploads the ZIP as a GitHub Release asset. The EXE/ZIP are not committed
to the repository.

Requirements:
- GitHub CLI: https://cli.github.com/
- One-time login: `gh auth login`

```powershell
.\scripts\publish_release.ps1
```

Packaging-only check without upload:

```powershell
.\scripts\publish_release.ps1 -SkipUpload -SkipBuild -SkipVerify -AllowDirty
```

## Stability soak profiles

These checks use simulated probes, so they do not require access to a real
company network.

```powershell
# Fast 50-target release smoke used by scripts\verify_release.py
python scripts\soak_test.py --profile release

# 30-minute 50-target stability check for longer local validation
python scripts\soak_test.py --profile long

# Offscreen MainWindow wiring check
python scripts\soak_test.py --profile ui
```

## 운영 개선 사항

- 장시간 측정 중 화면 그래프와 최근 관측치는 제한된 버퍼만 유지합니다.
- 전체 샘플은 `exports\session_logs`의 세션 로그에 스트리밍 저장되어 CSV/XLSX 저장 시 사용됩니다.
- CSV/XLSX/Report 저장은 백그라운드 작업으로 실행되어 큰 결과를 저장해도 UI 응답성을 유지합니다.
- IPv4 대상은 기본 최대 50개까지 측정하며, 초과 입력 시 처음 50개 사용 여부를 확인합니다.
- 선택된 IPv4 대상 Ping은 즉시 시작하고, Tracert 결과는 완료되는 대로 Hop 테이블에 반영합니다.
- 대상 그룹 JSON에는 이름, 생성 시각, 저장 출처, 대상 수, 측정 설정 요약, 대상별 주기 override가 남아 여러 그룹 파일을 재사용하기 쉽습니다.
- 알림 프리셋 JSON에는 이름, 생성 시각, 활성 조건/액션 요약이 남고, 로드 전에 요약과 실제 설정이 맞는지 검증합니다.
- Session Manager와 세션 ZIP manifest에 Mode, Engine, TCP Port가 분리되어 남아 재개/감사 시 확인하기 쉽습니다.
- 저장된 세션에서 Resume 후 Start하면 새 세션에 원본 세션 ID가 남아 이어 측정한 이력을 추적할 수 있습니다.
- 세션 로그 저장 오류가 발생하면 세션이 `Pause` 상태와 원인 코드로 남아 다음 실행에서 원인을 확인할 수 있습니다.
- Session Manager 새로고침은 기존 세션의 CSV 세그먼트를 다시 읽어 샘플 수, 마지막 시각, 대상 수, 세그먼트 목록을 보정합니다.
- EXE 빌드는 사용하지 않는 대형 모듈을 제외해 배포 크기를 줄입니다.

## 주의

중간 Hop의 packet loss는 실제 장애가 아니라 ICMP rate limit 또는 방화벽 정책일 수 있습니다.
이 도구의 분석 결과는 확정 진단이 아니라 장애 가능성 판단을 돕기 위한 참고 정보입니다.
