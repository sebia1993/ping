from __future__ import annotations

import json
import logging
from pathlib import Path

from app.developer.models import RequestRecord
from app.storage.atomic_write import atomic_write_path, read_text_with_retries
from app.utils.app_paths import app_data_directory


REQUEST_HISTORY_VERSION = 1
REQUEST_HISTORY_LIMIT = 50
REQUEST_HISTORY_FILE_NAME = "developer_requests.json"
PREFERENCES_FILE_NAME = "developer_preferences.json"
ENVIRONMENT_KINDS = ("사내 테스트 환경", "집 개발 환경", "기타 환경")


class DeveloperHistoryError(RuntimeError):
    pass


class RequestHistoryStore:
    def __init__(self, path: Path | None = None, *, limit: int = REQUEST_HISTORY_LIMIT) -> None:
        self.path = path or app_data_directory() / REQUEST_HISTORY_FILE_NAME
        self.limit = max(1, int(limit))
        self._load_error = False

    def load(self) -> list[RequestRecord]:
        self._load_error = False
        if not self.path.exists():
            return []
        try:
            payload = json.loads(read_text_with_retries(self.path))
            if not isinstance(payload, dict) or payload.get("version") != REQUEST_HISTORY_VERSION:
                raise ValueError("unsupported developer request history schema")
            records = payload.get("records")
            if not isinstance(records, list):
                raise ValueError("developer request history records must be a list")
            return [RequestRecord.from_mapping(item) for item in records if isinstance(item, dict)][: self.limit]
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            self._load_error = True
            logging.getLogger(__name__).warning(
                "Developer request history could not be loaded: %s",
                type(exc).__name__,
            )
            return []

    def save(self, records: list[RequestRecord]) -> None:
        if self._load_error and self.path.exists():
            raise DeveloperHistoryError("기존 요청 기록 파일이 손상되어 덮어쓰지 않았습니다.")
        payload = {
            "version": REQUEST_HISTORY_VERSION,
            "records": [record.as_dict() for record in records[: self.limit]],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        try:
            atomic_write_path(self.path, lambda temp_path: temp_path.write_text(text, encoding="utf-8"))
        except OSError as exc:
            raise DeveloperHistoryError("요청 기록을 저장할 수 없습니다.") from exc

    def add(self, record: RequestRecord) -> list[RequestRecord]:
        records = [item for item in self.load() if item.request_id != record.request_id]
        if self._load_error:
            raise DeveloperHistoryError("기존 요청 기록 파일이 손상되어 새 기록을 저장하지 않았습니다.")
        records.insert(0, record)
        records = records[: self.limit]
        self.save(records)
        return records

    def delete(self, request_id: str) -> list[RequestRecord]:
        records = [item for item in self.load() if item.request_id != request_id]
        if self._load_error:
            raise DeveloperHistoryError("기존 요청 기록 파일이 손상되어 삭제하지 않았습니다.")
        self.save(records)
        return records

    def clear(self) -> None:
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError as exc:
                raise DeveloperHistoryError("요청 기록 파일을 삭제할 수 없습니다.") from exc
        self._load_error = False


class DeveloperPreferencesStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_data_directory() / PREFERENCES_FILE_NAME

    def load_environment_kind(self) -> str:
        if not self.path.exists():
            return ENVIRONMENT_KINDS[0]
        try:
            payload = json.loads(read_text_with_retries(self.path))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return ENVIRONMENT_KINDS[0]
        value = payload.get("environment_kind") if isinstance(payload, dict) else None
        return str(value) if value in ENVIRONMENT_KINDS else ENVIRONMENT_KINDS[0]

    def save_environment_kind(self, environment_kind: str) -> None:
        value = environment_kind if environment_kind in ENVIRONMENT_KINDS else ENVIRONMENT_KINDS[0]
        text = json.dumps({"version": 1, "environment_kind": value}, ensure_ascii=False, indent=2)
        atomic_write_path(self.path, lambda temp_path: temp_path.write_text(text, encoding="utf-8"))
