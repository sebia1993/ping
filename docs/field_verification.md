# Field Verification Checklist

이 체크리스트는 회사망에서 실제 대상 정보를 외부로 공유하지 않고 로컬에서 검증하기 위한 절차입니다.
명령 출력, IP, 호스트명, 스크린샷은 외부로 전달하기 전에 마스킹하세요.

## 기본 릴리스 검증

```powershell
python scripts\verify_release.py
.\build_windows_exe.ps1
python scripts\verify_release.py --live --exe
```

## 장시간 안정성 검증
아래 명령은 실제 사내 장비 정보를 쓰지 않고 simulated probe로 다중 IP timeout 부하를 재현합니다.
결과 JSON의 `failures`가 비어 있고, `stopped_cleanly`가 `true`여야 합니다.

```powershell
python scripts\soak_test.py --profile long --duration-seconds 1800 --no-ui
python scripts\soak_test.py --profile long4h
python scripts\soak_test.py --profile long8h
python scripts\soak_test.py --profile long24h
python scripts\soak_test.py --profile ui10
python scripts\soak_test.py --profile ui20
python scripts\soak_test.py --profile ui50
```

주요 기준:
- `max_active_threads`는 40 이하
- `max_ui_event_gap_seconds`는 UI profile 기준 0.2초 이하
- `memory_growth_bytes`는 profile별 기준값 이하
- `cpu_percent`는 long profile 기준 80% 이하, 4/8/24시간 profile 기준 70% 이하
- `max_pending_ping_count`와 `max_log_queue_depth`가 실패 기준을 넘으면 안정성 회귀로 처리
- `session_log_rows`와 `session_log_segments`가 0이면 세션 저장 실패로 처리

## 사내 게이트웨이 또는 업무 사이트 검증

`<FIELD_TARGET>`을 사내 게이트웨이 IPv4 주소 또는 업무 사이트 IPv4 주소로 바꿔 로컬에서 실행합니다.
이 옵션은 읽기 전용 `ping`과 `tracert`만 수행합니다.

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
python scripts\verify_release.py --live --exe --target <FIELD_TARGET>
```

## GUI 동작 검증

1. `dist\MultiPingCheck\MultiPingCheck.exe`를 실행합니다.
2. 대상에 공인 IPv4 주소 또는 사내 게이트웨이 IPv4 주소를 입력합니다.
3. 주기를 `1`, `2`, `5`초로 각각 바꿔 시작합니다.
4. 시작 후 Hop 테이블이 채워지고 최종 대상 그래프가 움직이는지 확인합니다.
5. Start/Stop을 5회 반복하고 앱이 멈추거나 프로세스가 남지 않는지 확인합니다.
6. 30분 이상 무제한 측정 후 메모리 증가, UI 멈춤, 미종료 프로세스가 없는지 확인합니다.
7. CSV, XLSX, Report를 저장하고 파일명에 대상과 시간이 포함되는지 확인합니다.
8. 실시간 그래프에서 IP별 행, 상태 색상, 시간 범위 선택, 이름 버튼, 일시중지/삭제 버튼이 정상 동작하는지 확인합니다.

## 장애 케이스 검증

- 도메인 이름 또는 IPv6 주소: “IPv4 주소만 입력 가능합니다.” 메시지가 표시되고 대상 목록에 등록되지 않아야 합니다.
- 응답 없는 IP: timeout 또는 loss로 누적되어야 합니다.
- 중간 Hop timeout 포함 경로: 이후 Hop과 최종 대상이 정상인 경우 ICMP 응답 제한 가능성 문구가 표시되어야 합니다.
- 첫 Hop부터 손실: 단말/무선/AP/게이트웨이 구간 문제 가능성 문구가 표시되어야 합니다.
- IPv4 50개 초과 입력: 시작 시 처음 50개 사용 여부 확인 팝업이 표시되어야 합니다.
- 느린 Tracert 경로: Target Monitor와 그래프가 먼저 갱신되고, Hop 테이블은 Tracert 완료 후 채워져야 합니다.
- 장시간 측정: 화면 그래프는 최근 데이터 중심으로 유지되고 `exports\session_logs`에 세션 샘플 로그가 생성되어야 합니다.
- 큰 CSV/XLSX 저장: 저장 중에도 UI가 멈추지 않고 상태줄에 저장 진행/완료 메시지가 표시되어야 합니다.

## 기록 기준

- 원시 장비 출력이나 실제 고객 정보는 저장하지 않습니다.
- 외부 공유가 필요한 경우 대상 IP/호스트명, 사용자명, SSID, 사이트명을 마스킹합니다.
- 분석 결과는 확정 진단이 아니라 “가능성” 문구로 기록합니다.
