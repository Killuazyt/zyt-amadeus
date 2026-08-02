from __future__ import annotations

import json

from amadeus_desktop.paths import AppPaths
from amadeus_desktop.tools.persona import main


def _knowledge_line(
    content: str,
    *,
    source_ref: str,
    source_hash: str,
) -> str:
    return json.dumps(
        {
            "content": content,
            "tags": ["private-tag"],
            "source_ref": source_ref,
            "source_hash": source_hash,
        },
        ensure_ascii=False,
    )


def _configure_local_data(monkeypatch, tmp_path) -> AppPaths:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return AppPaths.for_current_user()


def _result(capsys) -> tuple[dict[str, object], str]:
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.out + captured.err


def test_import_and_status_emit_only_aggregate_count(monkeypatch, tmp_path, capsys) -> None:
    paths = _configure_local_data(monkeypatch, tmp_path)
    private_content = "PRIVATE PERSONA FRAGMENT"
    private_source = "PRIVATE/REFERENCE/PATH.txt"
    private_hash = "a" * 64
    paths.persona_knowledge_file.parent.mkdir(parents=True)
    paths.persona_knowledge_file.write_text(
        "\n".join(
            (
                _knowledge_line(
                    private_content,
                    source_ref=private_source,
                    source_hash=private_hash,
                ),
                _knowledge_line(
                    "SECOND PRIVATE FRAGMENT",
                    source_ref=private_source,
                    source_hash="b" * 64,
                ),
            )
        ),
        encoding="utf-8",
    )

    assert main(["import"]) == 0
    imported, import_output = _result(capsys)
    assert imported == {"count": 2, "error_category": None}
    assert private_content not in import_output
    assert private_source not in import_output
    assert private_hash not in import_output
    assert "private-tag" not in import_output

    assert main(["status"]) == 0
    status, status_output = _result(capsys)
    assert status == {"count": 2, "error_category": None}
    assert private_content not in status_output
    assert private_source not in status_output
    assert private_hash not in status_output
    assert "private-tag" not in status_output


def test_import_reports_safe_loader_category_without_private_values(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    paths = _configure_local_data(monkeypatch, tmp_path)
    private_value = "PRIVATE BODY MUST NOT LEAK"
    paths.persona_knowledge_file.parent.mkdir(parents=True)
    paths.persona_knowledge_file.write_text(
        '{"content":"' + private_value + '","unexpected":true}',
        encoding="utf-8",
    )

    assert main(["import"]) == 1
    result, output = _result(capsys)
    assert result == {"count": 0, "error_category": "invalid_schema"}
    assert private_value not in output
    assert str(paths.persona_knowledge_file) not in output


def test_missing_local_file_reports_only_safe_category(monkeypatch, tmp_path, capsys) -> None:
    paths = _configure_local_data(monkeypatch, tmp_path)

    assert main(["import"]) == 1
    result, output = _result(capsys)
    assert result == {"count": 0, "error_category": "io_error"}
    assert str(paths.persona_knowledge_file) not in output
