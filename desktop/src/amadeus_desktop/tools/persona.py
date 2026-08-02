"""Offline import and status CLI for private local persona knowledge."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections.abc import Sequence

from amadeus_desktop.database import DatabaseError, SQLiteDatabase
from amadeus_desktop.paths import AppPaths
from amadeus_desktop.persona_loader import PersonaKnowledgeLoadError, load_persona_knowledge_jsonl
from amadeus_desktop.persona_repository import PersonaRepository
from amadeus_desktop.storage_models import StorageError

PERSONA_ID = "kurisu"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amadeus-persona",
        description="Import or inspect private local persona knowledge without network access.",
    )
    parser.add_argument("operation", choices=("import", "status"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    operation = build_parser().parse_args(argv).operation
    paths = AppPaths.for_current_user()
    database: SQLiteDatabase | None = None
    try:
        database = SQLiteDatabase(
            paths.database_file,
            backup_dir=paths.migration_backup_directory,
        ).open()
        if database.read_only:
            _print_result(0, "database_read_only")
            return 1

        repository = PersonaRepository(database)
        if operation == "import":
            drafts = load_persona_knowledge_jsonl(
                paths.persona_knowledge_file,
                persona_id=PERSONA_ID,
            )
            count = len(repository.replace_persona(PERSONA_ID, drafts))
        else:
            count = len(repository.list_active_documents(PERSONA_ID, limit=10_000))
    except PersonaKnowledgeLoadError as exc:
        _print_result(0, str(exc.code))
        return 1
    except StorageError:
        _print_result(0, "storage_error")
        return 1
    except (DatabaseError, sqlite3.Error):
        _print_result(0, "database_error")
        return 1
    except Exception:
        # Do not expose exception text: a dependency failure may include a
        # private local path or a fragment value.
        _print_result(0, "operation_failed")
        return 1
    finally:
        if database is not None:
            database.close()

    _print_result(count, None)
    return 0


def _print_result(count: int, error_category: str | None) -> None:
    """Emit only an aggregate count and a bounded local error category."""

    print(
        json.dumps(
            {"count": max(0, int(count)), "error_category": error_category},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
