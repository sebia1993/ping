from __future__ import annotations

import json
import subprocess
import sys

from app.developer.build_info import CONFIG_SCHEMA_VERSION, load_build_info


def test_load_build_info_reads_embedded_json_without_git_lookup(tmp_path) -> None:
    path = tmp_path / "multipingcheck_build_info.json"
    path.write_text(
        json.dumps(
            {
                "program_name": "MultiPingCheck",
                "program_version": "1.4.2",
                "build_id": "20260813-01",
                "build_time": "2026-08-13T10:25:41+09:00",
                "git_commit": "a81c4f2",
                "git_branch": "main",
                "distribution": "Windows Portable EXE",
                "config_schema_version": "2",
                "source_state": "커밋과 일치",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    info = load_build_info(path)

    assert info.program_version == "1.4.2"
    assert info.build_id == "20260813-01"
    assert info.git_commit == "a81c4f2"
    assert info.distribution == "Windows Portable EXE"


def test_generate_build_info_script_writes_traceable_metadata(tmp_path) -> None:
    output = tmp_path / "build.json"

    completed = subprocess.run(
        [sys.executable, "scripts/generate_build_info.py", "--output", str(output), "--build-id", "test-build"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["build_id"] == "test-build"
    assert payload["config_schema_version"] == CONFIG_SCHEMA_VERSION
    assert payload["distribution"] == "Windows Portable EXE"
    assert payload["git_commit"]
    assert payload["git_branch"]
    assert payload["source_state"] in {"커밋과 일치", "미커밋 변경 포함", "확인 불가"}


def test_build_script_embeds_generated_metadata() -> None:
    text = open("build_windows_exe.ps1", encoding="utf-8").read()

    assert "scripts\\generate_build_info.py" in text
    assert "--add-data \"$BuildInfoPath;.\"" in text
    assert "multipingcheck_build_info.json" in text
