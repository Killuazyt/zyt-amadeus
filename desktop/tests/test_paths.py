from __future__ import annotations

from pathlib import Path

from amadeus_desktop.paths import AppDirectory, AppPaths


def test_current_user_path_uses_local_app_data(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    paths = AppPaths.for_current_user()

    assert paths.root == tmp_path / "Amadeus"


def test_initialize_creates_only_p1_startup_directories(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)

    paths.initialize()

    assert paths.root.is_dir()
    assert paths.directory(AppDirectory.CONFIG).is_dir()
    assert paths.directory(AppDirectory.LOGS).is_dir()
    assert not paths.directory(AppDirectory.DATA).exists()
    assert not paths.directory(AppDirectory.PETS).exists()


def test_other_regions_are_created_on_demand(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)

    created = paths.ensure(AppDirectory.BACKUPS)

    assert created == tmp_path / "Amadeus" / "backups"
    assert created.is_dir()


def test_p5b_model_and_persona_paths_stay_under_local_app_data(tmp_path: Path) -> None:
    paths = AppPaths.for_current_user(tmp_path)

    assert paths.embedding_model_directory == (
        tmp_path
        / "Amadeus"
        / "models"
        / "bge-small-zh-v1.5"
        / "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
    )
    assert paths.persona_knowledge_file == (
        tmp_path / "Amadeus" / "personas" / "kurisu" / "knowledge.jsonl"
    )
