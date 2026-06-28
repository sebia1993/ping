# GitHub 인증 설정

이 문서는 로컬 Mac 또는 Windows PC에서 `git push`와 Release 업로드가 막힐 때 확인할 절차입니다.

## 현재 증상

다음 오류가 나오면 GitHub 인증 정보가 로컬 Git에 연결되지 않은 상태입니다.

```text
fatal: could not read Username for 'https://github.com': Device not configured
```

## 권장 설정

1. GitHub CLI를 설치합니다.
   - Homebrew가 있으면 `brew install gh`
   - Homebrew가 없으면 https://cli.github.com/ 에서 설치 파일을 받습니다.
2. 브라우저 로그인으로 인증합니다.

```bash
gh auth login
```

3. Git이 GitHub CLI 인증을 사용하도록 연결합니다.

```bash
gh auth setup-git
```

4. 상태를 확인합니다.

```bash
gh auth status
git push --dry-run origin HEAD
```

## Release 자동화와의 관계

- GitHub Actions의 `Release Windows ZIP` workflow는 `GITHUB_TOKEN`을 사용하므로 로컬 `gh auth login` 없이도 Release asset을 올릴 수 있습니다.
- 로컬 PC에서 `scripts\publish_release.ps1`로 직접 Release를 올릴 때는 `gh auth login`이 필요합니다.
- 일반 소스 동기화를 위해 `git push`를 자주 쓸 경우에도 이 설정을 완료하는 것이 좋습니다.
