from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QLockFile

from app.utils.app_paths import app_data_directory


INSTANCE_LOCK_FILE_NAME = "multipingcheck.instance.lock"
APP_ALREADY_RUNNING_CODE = "APP_ALREADY_RUNNING"
APP_INSTANCE_LOCK_FAILED_CODE = "APP_INSTANCE_LOCK_FAILED"


class InstanceLockError(RuntimeError):
    """프로그램 중복 실행 방지 잠금을 얻지 못했을 때 사용하는 안정 오류입니다."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def acquire_instance_lock(root: Path | None = None) -> QLockFile:
    """동일한 사용자 데이터 폴더를 쓰는 프로그램이 한 번만 실행되게 합니다."""

    lock_root = root or app_data_directory()
    try:
        lock_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InstanceLockError(
            APP_INSTANCE_LOCK_FAILED_CODE,
            f"프로그램 데이터 폴더를 준비하지 못했습니다: {type(exc).__name__}",
        ) from exc

    lock = QLockFile(str(lock_root / INSTANCE_LOCK_FILE_NAME))
    # 정상 실행이 오래 지속돼도 파일 나이만으로 잠금을 제거하지 않습니다.
    # QLockFile은 종료된 PID의 stale lock은 별도로 판별할 수 있습니다.
    lock.setStaleLockTime(0)
    if lock.tryLock(0):
        return lock

    if lock.error() == QLockFile.LockError.LockFailedError:
        raise InstanceLockError(
            APP_ALREADY_RUNNING_CODE,
            "멀티핑체크가 이미 실행 중입니다.",
        )
    raise InstanceLockError(
        APP_INSTANCE_LOCK_FAILED_CODE,
        "프로그램 중복 실행 방지 잠금을 만들 수 없습니다.",
    )
