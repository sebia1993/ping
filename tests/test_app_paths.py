from __future__ import annotations

from datetime import datetime

from app.core.models import STATUS_OK, HopObservation
from app.storage.session_index import SessionIndexStore
from app.storage.session_log import SessionLogWriter
from app.utils.app_paths import (
    app_data_directory,
    diagnostic_logs_directory,
    session_logs_directory,
    user_exports_directory,
)
from app.utils.filename import default_export_path


def test_application_paths_use_explicit_user_overrides(monkeypatch, tmp_path) -> None:
    data_root = tmp_path / "사용자 데이터"
    export_root = tmp_path / "사용자 내보내기"
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(data_root))
    monkeypatch.setenv("MULTIPINGCHECK_EXPORT_DIR", str(export_root))

    assert app_data_directory() == data_root
    assert session_logs_directory() == data_root / "session_logs"
    assert diagnostic_logs_directory() == data_root / "logs"
    assert user_exports_directory() == export_root
    assert default_export_path("192.0.2.1", "csv").parent == export_root


def test_default_session_storage_does_not_depend_on_current_working_directory(
    monkeypatch,
    tmp_path,
) -> None:
    data_root = tmp_path / "app-data"
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(data_root))
    monkeypatch.chdir(unrelated_cwd)

    store = SessionIndexStore.create()
    writer = SessionLogWriter.create("192.0.2.1")
    writer.close()

    assert store.path == data_root / "session_logs" / "session_index.json"
    assert writer.path.is_relative_to(data_root / "session_logs")
    assert not (unrelated_cwd / "exports").exists()


def test_new_session_index_imports_existing_legacy_session_files(monkeypatch, tmp_path) -> None:
    data_root = tmp_path / "app-data"
    legacy_cwd = tmp_path / "portable-app"
    legacy_root = legacy_cwd / "exports" / "session_logs"
    legacy_cwd.mkdir()
    monkeypatch.setenv("MULTIPINGCHECK_DATA_DIR", str(data_root))
    monkeypatch.setenv("MULTIPINGCHECK_LEGACY_SESSION_DIR", str(legacy_root))
    monkeypatch.chdir(legacy_cwd)

    legacy_writer = SessionLogWriter.create("198.51.100.10", root=legacy_root)
    legacy_path = legacy_writer.path
    legacy_writer.write_many(
        [
            HopObservation(
                datetime(2026, 1, 1, 12, 0, 0),
                0,
                "198.51.100.10",
                "Target",
                True,
                10.0,
                STATUS_OK,
                True,
            )
        ]
    )
    legacy_writer.close()
    legacy_store = SessionIndexStore.create(legacy_root)
    legacy_store.register_session(
        target="198.51.100.10",
        sample_path=legacy_path,
        route_path=None,
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        interval_seconds=1,
        measurement_mode="final_hop_only:icmp",
        target_count=1,
    )

    store = SessionIndexStore.create()
    sessions = store.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].sample_path == legacy_path
    assert store.path == data_root / "session_logs" / "session_index.json"
