# MultiPingCheck 프로젝트 상태

이 문서는 현재 구현 범위와 앞으로의 개선 우선순위를 간단히 기록합니다.

## 현재 범위

현재 기본 사용자 흐름은 다음에 집중합니다.

```text
IPv4 목록 입력
→ 다중 대상 측정 시작
→ 대상별 상태·실시간 그래프 확인
→ 공통 시간 범위 전환
→ 세션 저장·재열기
→ 필요 시 내보내기
```

기본 화면에서 사용자가 직접 다루는 핵심은 ICMP 기반 최종 대상 측정입니다.

## 현재 구현된 핵심

### 다중 대상 측정

- IPv4 최대 50개
- 대상별 상태와 그래프
- 측정 중 대상 추가
- 개별 이름 지정
- 개별 일시중지/삭제
- 공통 그래프 시간 범위

### 세션 저장

- segmented CSV
- Session Index
- 대상·월 bucket 구조
- stale session recovery
- 실제 파일과 index 재조정
- 부분 손상 복구 기록
- 세션 검색·열기·삭제·보관 기간 정리

### 분석과 Alert

- 최종 대상 중심 지연/손실 해석
- 중간 Hop ICMP rate-limit 가능성 구분
- loss/latency/jitter/sample/timer/MOS 조건
- Route 변경 관련 분석 경로
- Alert 시작/복구 event

고급 Alert action과 전체 경로/TCP 관련 기능은 코드에 유지되어 있으나 기본 UI의 핵심 사용 흐름보다 우선하지 않습니다.

### Export

- CSV
- XLSX
- TXT/HTML report
- PNG
- Statistics export
- Session export

### 안정성 검증

- 기본 Release verifier
- deterministic 50-target soak
- 30분 50-target profile
- 4/8/24시간 profile
- UI 10/20/50 target profile
- Worker/thread/memory/pending ping/log queue/session rows/UI event gap 검증

## 현재 중요한 설계 특성

1. **UI thread와 측정/파일 I/O를 분리**합니다.
2. **실시간 메모리와 장기 세션 저장소를 분리**합니다.
3. **오래된 background loader 결과가 현재 UI를 덮지 않도록 lifecycle을 관리**합니다.
4. **저장 실패와 부분 손상을 정상 결과처럼 숨기지 않습니다.**
5. **실제 네트워크 없이도 최대 대상 부하를 재현할 수 있게 simulated probe를 유지**합니다.
6. **최종 대상 상태를 우선해 중간 Hop의 ICMP 응답 제한을 장애로 과대 해석하지 않습니다.**

## 개선 우선순위

향후 개선은 기능 확장보다 다음 순서를 우선합니다.

1. 실제 장시간 사용에서 발견되는 lifecycle/저장 회귀의 재현 테스트화
2. 50-target 장시간 UI 렌더링 비용 최적화
3. 세션 이력 탐색성과 저장소 관리 개선
4. 장애 시간대의 여러 대상 상관관계 표시 강화
5. Export/보고서의 운영 증거 전달력 개선
6. Alert 설정의 운영자 안내 강화

## 비우선 항목

다음은 현재 우선순위가 낮습니다.

- 디자인을 위한 대규모 UI 재작성
- 대상 수를 검증 없이 50대 이상으로 확대
- 실제 사내 장비를 자동 CI 의존성으로 추가
- 기본 화면에 모든 고급 기능 노출
- 단일 관측으로 장애 원인을 자동 확정하는 기능

## 검증 기준

변경을 완료했다고 판단하려면 최소한:

```powershell
python scripts\verify_release.py
```

가 통과해야 합니다.

Windows package 관련 변경은 추가로:

```powershell
.\build_windows_exe.ps1
python scripts\verify_release.py --exe
```

를 확인합니다.

장시간 안정성 변경은 `docs/stability_soak.md`의 관련 profile을 별도로 실행합니다.
