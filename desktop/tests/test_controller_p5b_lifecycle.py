from __future__ import annotations

from types import SimpleNamespace

from amadeus_desktop.controller import ApplicationController


class _FakeVectorIndex:
    def __init__(self) -> None:
        self.refresh_count = 0

    def refresh_incremental(self) -> bool:
        self.refresh_count += 1
        return True


class _FakeTimer:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.start_count = 0

    def isActive(self) -> bool:  # noqa: N802 - mirrors the Qt API
        return self.active

    def start(self) -> None:
        self.active = True
        self.start_count += 1


class _FakeMemoryJobs:
    def __init__(self) -> None:
        self.resume_count = 0

    def resume(self) -> None:
        self.resume_count += 1


class _FakeModelSettings:
    def __init__(self) -> None:
        self.results: list[tuple[bool, str]] = []

    def apply_save_result(self, *, success: bool, message: str) -> None:
        self.results.append((success, message))


def _bare_controller() -> ApplicationController:
    return ApplicationController.__new__(ApplicationController)


def test_vector_model_ready_recovery_requests_one_incremental_refresh() -> None:
    controller = _bare_controller()
    controller._initial_index_refresh_requested = False
    controller._vector_index_available = False
    controller.vector_index = _FakeVectorIndex()

    controller._on_vector_index_status_changed(SimpleNamespace(category="ready", available=True))
    controller._on_vector_index_status_changed(SimpleNamespace(category="ready", available=True))
    controller._on_vector_index_status_changed(
        SimpleNamespace(category="incremental", available=True)
    )
    assert controller.vector_index.refresh_count == 1

    controller._on_vector_index_status_changed(
        SimpleNamespace(category="model_missing", available=False)
    )
    controller._on_vector_index_status_changed(SimpleNamespace(category="loading", available=True))
    assert controller.vector_index.refresh_count == 1
    controller._on_vector_index_status_changed(SimpleNamespace(category="ready", available=True))
    assert controller.vector_index.refresh_count == 2


def test_provider_switch_timeout_restores_stopped_maintenance_timer() -> None:
    controller = _bare_controller()
    controller._provider_switch_pending = True
    controller._provider_switch_generation = 7
    controller._pending_provider_configuration = object()
    controller._pending_provider_secret = "not-a-real-secret"
    controller._data_writable = True
    controller.memory_maintenance_timer = _FakeTimer()
    controller.memory_jobs = _FakeMemoryJobs()
    controller.model_settings_window = _FakeModelSettings()

    controller._on_provider_switch_timeout(7)

    assert controller.memory_maintenance_timer.start_count == 1
    assert controller.memory_jobs.resume_count == 1
    assert controller._pending_provider_configuration is None
    assert controller._pending_provider_secret is None
    assert controller._provider_switch_pending is False
    assert controller.model_settings_window.results == [
        (False, "后台记忆任务未能及时停止，模型配置未更改。")
    ]
