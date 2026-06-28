# 릴리즈 체크리스트

이 문서는 Windows ZIP Release를 만들 때 확인할 최소 절차입니다.

## 1. 소스 검증

- PR 또는 `main`/`stability/**` push에서 `Windows Fast Check` workflow가 성공했는지 확인합니다.
- 로컬에서 확인할 때는 다음 명령을 실행합니다.

```powershell
python scripts\verify_release.py
```

## 2. 최종 Windows EXE 검증

- GitHub Actions에서 `Windows Release Verify` workflow를 수동 실행합니다.
- 성공 조건:
  - source release verifier 성공
  - Windows EXE build 성공
  - packaged EXE smoke 성공
  - `MultiPingCheck-windows-<run_id>` artifact 업로드 성공

## 3. GitHub Release 생성

- `main` 브랜치에서 `Release Windows ZIP` workflow를 수동 실행합니다.
- `tag`, `title`, `notes`는 선택값입니다.
- `tag`를 비우면 KST 기준 `vYYYY.MM.DD-HHMMSS` 형식으로 자동 생성됩니다.
- workflow가 성공하면 GitHub Release에 다음 asset이 올라와야 합니다.
  - `MultiPingCheck_<tag>.zip`
  - `MultiPingCheck_<tag>.zip.sha256`

## 4. 다운로드 확인

- GitHub Release에서 ZIP과 `.sha256` 파일을 내려받습니다.
- ZIP을 압축 해제한 뒤 `README-실행안내.txt`가 포함되어 있는지 확인합니다.
- 압축 해제한 폴더의 `MultiPingCheck.exe`를 실행합니다.
- Python, PySide6, GitHub CLI 설치 없이 실행되어야 합니다.

## 5. 사용자 안내

- ZIP 안에서 바로 실행하지 말고 먼저 압축 해제해야 한다고 안내합니다.
- Windows SmartScreen 또는 Defender 경고가 처음 실행 시 표시될 수 있다고 안내합니다.
- 코드서명 또는 installer/MSIX는 별도 개선 항목으로 관리합니다.
