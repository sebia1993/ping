# 장시간 안정성 검증 가이드

이 문서는 MultiPingCheck를 오래 켜 두었을 때 멈춤, 누락, 메모리 증가,
thread 잔류, 세션 로그 누락이 생기는지 확인하기 위한 절차입니다.

실제 회사 장비나 인터넷 ping 대상은 사용하지 않습니다. 모든 장시간 검증은
`scripts/soak_test.py`의 simulated probe로 실행합니다.

## 로컬 또는 MacBook에서 실행

짧은 릴리즈 smoke:

```powershell
python scripts\soak_test.py --profile release
```

전체 장시간 suite 계획만 확인:

```powershell
python scripts\run_stability_soak_suite.py --dry-run
```

4시간, 8시간, 24시간, UI 10/20/50 대상 검증:

```powershell
python scripts\run_stability_soak_suite.py
```

중간에 PC가 꺼졌거나 일부 profile만 완료된 경우:

```powershell
python scripts\run_stability_soak_suite.py --resume --run-id <RUN_ID>
```

완료된 결과가 통과 증거인지 다시 확인:

```powershell
python scripts\run_stability_soak_suite.py --validate-only --run-id <RUN_ID>
```

## GitHub Actions에서 수동 실행

GitHub 저장소에서 다음 메뉴를 사용합니다.

1. `Actions`
2. `Manual Stability Soak`
3. `Run workflow`
4. `profiles` 입력
5. runner 선택
6. `Run workflow` 클릭

추천 입력:

- 빠른 확인: `release`
- UI 멈춤 수치 확인: `ui10 ui20 ui50`
- 4시간 확인: `long4h`
- 8시간/24시간 확인: `long8h long24h`

GitHub-hosted Windows runner는 짧은 확인이나 4시간 이하 검증에 적합합니다.
8시간/24시간 검증은 `self-hosted-windows` runner 또는 로컬 PC에서 실행하는
방식을 권장합니다.

참고:

- GitHub-hosted runner job 실행 한도:
  https://docs.github.com/en/actions/reference/limits
- GitHub Actions 과금/무료 사용 기준:
  https://docs.github.com/en/actions/concepts/billing-and-usage

## 통과 기준

결과 JSON의 `failures`가 빈 배열이어야 합니다.

주요 확인 항목:

- `stopped_cleanly`: worker가 정상 종료됐는지
- `session_log_rows`: 완료된 ping 결과가 세션 로그에 누락 없이 저장됐는지
- `max_active_threads`: thread 수가 비정상적으로 늘지 않았는지
- `memory_growth_bytes`: 메모리가 계속 증가하지 않았는지
- `max_ui_event_gap_seconds`: UI 이벤트 사이 간격이 기준을 넘지 않았는지
- `max_ui_event_process_seconds`: UI 이벤트 처리 1회가 오래 걸리지 않았는지
- `max_pending_ping_count`: 대기 중 ping이 계속 쌓이지 않았는지
- `max_log_queue_depth`: 세션 로그 저장 queue가 밀리지 않았는지

UI 10/20/50 profile은 `max_ui_event_gap_seconds`와
`max_ui_event_process_seconds`가 0.2초 이하인지 확인합니다.

## 커밋하지 말아야 할 것

다음 폴더는 검증 산출물입니다. Git 커밋에 포함하지 않습니다.

- `artifacts/`
- `release/`
- `dist/`
- `build/`
- `exports/`
- `logs/`
