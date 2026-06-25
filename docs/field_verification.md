# 현장 검증 체크리스트

이 문서는 회사망의 실제 장비 정보, 고객 정보, IP 목록, 로그 원문을 외부로 공유하지 않고
로컬 PC에서 프로그램 안정성을 검증하기 위한 절차입니다. 결과를 공유해야 할 때는 IP, 호스트명,
사용자명, 사이트명, 스크린샷의 민감한 영역을 먼저 가려야 합니다.

## 기본 릴리즈 검증

```powershell
python scripts\verify_release.py
.\build_windows_exe.ps1
python scripts\verify_release.py --live --exe
```

## 장시간 안정성 검증

아래 명령은 실제 네트워크에 ping을 보내지 않습니다. simulated probe로 다중 IP timeout 상황을
재현하면서 CPU, 메모리 증가, thread 잔류, session log 누락, UI event gap을 확인합니다.

전체 장시간 검증을 한 번에 계획하거나 실행할 때는 suite 명령을 사용합니다.

```powershell
# 실행 계획과 manifest만 확인합니다. 실제 4/8/24시간 측정은 시작하지 않습니다.
python scripts\run_stability_soak_suite.py --dry-run

# 4시간, 8시간, 24시간, UI 10/20/50대 검증을 순서대로 실행합니다.
python scripts\run_stability_soak_suite.py

# 같은 run-id로 중간에 끊긴 검증을 이어서 실행합니다.
python scripts\run_stability_soak_suite.py --resume --run-id <RUN_ID>

# 기존 manifest와 summary JSON이 실제 통과 증거인지 다시 검증합니다.
python scripts\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>
```

개별 프로필만 따로 실행할 수도 있습니다.

```powershell
python scripts\soak_test.py --profile long --duration-seconds 1800 --no-ui
python scripts\soak_test.py --profile long4h
python scripts\soak_test.py --profile long8h
python scripts\soak_test.py --profile long24h
python scripts\soak_test.py --profile ui10
python scripts\soak_test.py --profile ui20
python scripts\soak_test.py --profile ui50
```

주요 통과 기준:

- 결과 JSON의 `failures`가 빈 배열이어야 합니다.
- `stopped_cleanly`가 `true`여야 합니다.
- `max_active_threads`는 프로필 기준 이하이어야 합니다.
- UI 프로필의 `max_ui_event_gap_seconds`는 0.2초 이하이어야 합니다.
- `memory_growth_bytes`는 프로필별 기준값 이하이어야 합니다.
- `cpu_percent`는 long 프로필 기준 이하이어야 합니다.
- `session_log_rows`와 `session_log_segments`가 0이면 세션 저장 실패로 봅니다.
- `max_pending_ping_count`와 `max_log_queue_depth`가 기준을 넘으면 장시간 부하 위험으로 봅니다.

## 사내 게이트웨이 또는 업무 사이트 검증

`<FIELD_TARGET>`은 사내 게이트웨이 IPv4 주소 또는 업무 사이트 IPv4 주소로 바꿔 실행합니다.
이 검증은 읽기 전용 `ping`과 `tracert`만 사용합니다.

```powershell
python scripts\verify_release.py --target <FIELD_TARGET>
python scripts\verify_release.py --live --exe --target <FIELD_TARGET>
```

## GUI 동작 검증

1. `dist\MultiPingCheck\MultiPingCheck.exe`를 실행합니다.
2. 공인 IPv4 주소 또는 사내 게이트웨이 IPv4 주소를 입력합니다.
3. 주기를 `1`, `2`, `5`초로 바꿔가며 시작합니다.
4. 시작 후 실시간 그래프가 대상별 행으로 표시되는지 확인합니다.
5. Start/Stop을 5회 반복하고 UI가 멈추거나 프로세스가 남지 않는지 확인합니다.
6. 30분 이상 측정하면서 메모리 증가, UI 멈춤, 미종료 프로세스가 없는지 확인합니다.
7. CSV, XLSX, Report 저장 중 UI가 멈추지 않고 완료 메시지가 나오는지 확인합니다.
8. 실시간 그래프에서 IP별 상태 색상, 시간 범위 선택, 이름 버튼, 일시중지/삭제 버튼이 동작하는지 확인합니다.

## 역할 케이스 검증

- 도메인 이름 또는 IPv6 주소: "IPv4 주소만 입력 가능합니다." 메시지가 나오고 대상 목록에 등록되지 않아야 합니다.
- 응답 없는 IP: timeout 또는 loss로 누적되어야 합니다.
- 중간 Hop timeout 포함 경로: 이후 Hop과 최종 대상이 정상이면 ICMP 응답 제한 가능성 문구가 나와야 합니다.
- 첫 Hop부터 손실: 단말, 무선, AP, 게이트웨이 구간 문제 가능성 문구가 나와야 합니다.
- IPv4 50개 초과 입력: 시작 전 처음 50개 사용 여부 확인 팝업이 나와야 합니다.
- 느린 Tracert 경로: 실시간 그래프가 먼저 갱신되고 Hop 테이블은 Tracert 완료 후 채워져야 합니다.
- 장시간 측정: 그래프는 최근 데이터 중심으로 표시되고 `exports\session_logs`에 세션 샘플 로그가 생성되어야 합니다.

## 기록 기준

- 실제 장비 출력이나 실제 고객 정보는 저장하지 않습니다.
- 외부 공유가 필요한 경우 IP, 호스트명, 사용자명, SSID, 사이트명을 마스킹합니다.
- 분석 결과는 확정 진단이 아니라 가능성 판단으로 기록합니다.
- 장시간 soak 결과는 `artifacts\stability_soak_suite` 또는 명령에서 지정한 output 폴더의 JSON manifest를 기준으로 확인합니다.
