from __future__ import annotations

import threading

from amadeus_desktop.data_runtime import DataPriority, SerialDataThread


def test_serial_data_thread_owns_resource_and_returns_on_ui_thread(qtbot) -> None:
    factory_thread: list[int] = []
    operation_threads: list[int] = []
    callback_threads: list[int] = []
    results: list[int] = []

    def factory() -> list[int]:
        factory_thread.append(threading.get_ident())
        return []

    runtime = SerialDataThread(factory)
    runtime.start()
    qtbot.waitUntil(lambda: runtime.is_ready)

    def operation(resource: list[int]) -> int:
        operation_threads.append(threading.get_ident())
        resource.append(7)
        return resource[-1]

    runtime.submit(
        operation,
        priority=DataPriority.FOREGROUND,
        on_success=lambda value: (
            callback_threads.append(threading.get_ident()),
            results.append(value),
        ),
    )
    qtbot.waitUntil(lambda: results == [7])

    assert factory_thread == operation_threads
    assert factory_thread[0] != threading.get_ident()
    assert callback_threads == [threading.get_ident()]
    assert runtime.shutdown(1_000)


def test_serial_data_thread_redacts_exception_text(qtbot) -> None:
    failures: list[str] = []
    runtime = SerialDataThread(lambda: object())
    runtime.start()
    qtbot.waitUntil(lambda: runtime.is_ready)

    def fail(_resource: object) -> None:
        raise RuntimeError("private conversation body must not cross the boundary")

    runtime.submit(fail, on_failure=failures.append)
    qtbot.waitUntil(lambda: bool(failures))

    assert failures == ["RuntimeError"]
    assert runtime.shutdown(1_000)


def test_initialization_failure_stops_accepting_new_work(qtbot) -> None:
    categories: list[str] = []

    def fail_factory() -> object:
        raise RuntimeError("private initialization detail")

    runtime = SerialDataThread(fail_factory)
    runtime.initialization_failed.connect(categories.append)
    runtime.start()
    qtbot.waitUntil(lambda: bool(categories))

    assert categories == ["RuntimeError"]
    assert not runtime.is_running
    assert runtime.submit(lambda resource: resource) is None
    assert runtime.shutdown(1_000)
