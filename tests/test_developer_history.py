from __future__ import annotations

import json

import pytest

from app.developer.history import DeveloperHistoryError, DeveloperPreferencesStore, RequestHistoryStore
from app.developer.models import RequestRecord


def _record(index: int) -> RequestRecord:
    return RequestRecord(
        request_id=f"REQ-{index:03d}",
        request_type="기존 기능 변경",
        created_at="2026-08-13T10:00:00+09:00",
        screen_name="main_dashboard",
        ui_id="main_dashboard.measurement.start_button",
        feature_id="measurement.start",
        summary=f"요청 {index}",
        prompt=f"prompt {index}",
        draft={"kind": "feature", "desired_change": f"요청 {index}"},
    )


def test_request_history_keeps_newest_fifty_records(tmp_path) -> None:
    store = RequestHistoryStore(tmp_path / "history.json", limit=50)

    for index in range(55):
        store.add(_record(index))

    records = store.load()
    assert len(records) == 50
    assert records[0].request_id == "REQ-054"
    assert records[-1].request_id == "REQ-005"


def test_corrupted_request_history_is_not_overwritten(tmp_path) -> None:
    path = tmp_path / "history.json"
    path.write_text("{broken", encoding="utf-8")
    store = RequestHistoryStore(path)

    assert store.load() == []
    with pytest.raises(DeveloperHistoryError):
        store.add(_record(1))
    assert path.read_text(encoding="utf-8") == "{broken"


def test_environment_preference_uses_safe_default_and_persists_valid_value(tmp_path) -> None:
    path = tmp_path / "preferences.json"
    store = DeveloperPreferencesStore(path)

    assert store.load_environment_kind() == "사내 테스트 환경"
    store.save_environment_kind("집 개발 환경")
    assert store.load_environment_kind() == "집 개발 환경"

    path.write_text(json.dumps({"environment_kind": "잘못된 값"}, ensure_ascii=False), encoding="utf-8")
    assert store.load_environment_kind() == "사내 테스트 환경"
