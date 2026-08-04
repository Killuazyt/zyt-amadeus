from types import SimpleNamespace

import pytest

from amadeus_desktop import app
from amadeus_desktop.app import (
    _acceptance_instance_name,
    _auto_exit_delay,
    _embedding_lifecycle_mode,
    _ready_lifecycle_surface_is_valid,
    _wincred_acceptance_probe_id,
)
from amadeus_desktop.embedding_model import ModelAvailability, ModelVerification


def test_auto_exit_delay_is_bounded_and_hidden_from_normal_startup() -> None:
    assert _auto_exit_delay(()) is None
    assert _auto_exit_delay(("--auto-exit-ms=500",)) == 500
    assert _auto_exit_delay(("--auto-exit-ms=0",)) is None
    assert _auto_exit_delay(("--auto-exit-ms=60001",)) is None
    assert _auto_exit_delay(("--auto-exit-ms=private",)) is None


def test_embedding_lifecycle_mode_is_hidden_and_strict() -> None:
    assert _embedding_lifecycle_mode(()) is None
    assert _embedding_lifecycle_mode(("--embedding-lifecycle-probe=ready",)) == "ready"
    assert _embedding_lifecycle_mode(("--embedding-lifecycle-probe=degraded",)) == "degraded"
    assert _embedding_lifecycle_mode(("--embedding-lifecycle-probe=unknown",)) is None


def test_acceptance_instance_name_is_strict() -> None:
    valid = "amadeus-acceptance-" + "a" * 32
    assert _acceptance_instance_name((f"--acceptance-instance-name={valid}",)) == valid
    assert _acceptance_instance_name(("--acceptance-instance-name=production",)) is None


def test_wincred_acceptance_probe_id_is_single_lowercase_128_bit_hex() -> None:
    valid = "0123456789abcdef0123456789abcdef"
    assert _wincred_acceptance_probe_id((f"--wincred-acceptance-probe={valid}",)) == valid
    assert _wincred_acceptance_probe_id((f"--wincred-acceptance-probe={'A' * 32}",)) is None
    assert _wincred_acceptance_probe_id((f"--wincred-acceptance-probe={'a' * 31}",)) is None
    assert (
        _wincred_acceptance_probe_id((f"--wincred-acceptance-probe={valid}", "--mock-chat")) is None
    )


def test_frozen_wincred_probe_runs_before_build_info_qt_and_user_paths(monkeypatch) -> None:
    probe_id = "0123456789abcdef0123456789abcdef"
    calls: list[str] = []
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        app,
        "run_wincred_acceptance_probe",
        lambda value: calls.append(value) or True,
    )
    monkeypatch.setattr(app, "load_build_info", lambda: pytest.fail("must remain early"))

    assert app.main(("Amadeus", f"--wincred-acceptance-probe={probe_id}")) == 0
    assert calls == [probe_id]


def test_uninstall_cleanup_is_frozen_only_exact_and_early(monkeypatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(app.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app, "_run_uninstall_cleanup", lambda: calls.append(True) or 0)
    monkeypatch.setattr(app, "load_build_info", lambda: pytest.fail("must remain early"))

    assert app.main(("Amadeus", "--uninstall-cleanup=delete-data")) == 0
    assert calls == [True]
    assert app.main(("Amadeus", "--uninstall-cleanup=keep-data")) == 6
    assert app.main(("Amadeus", "--uninstall-cleanup=delete-data", "--mock-chat")) == 6


def test_source_process_cannot_invoke_destructive_uninstall_cleanup(monkeypatch) -> None:
    monkeypatch.delattr(app.sys, "frozen", raising=False)
    monkeypatch.setattr(
        app,
        "_run_uninstall_cleanup",
        lambda: pytest.fail("source process must not delete data"),
    )

    assert app.main(("Amadeus", "--uninstall-cleanup=delete-data")) == 6


def test_embedding_probe_runs_before_qt_and_returns_only_an_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(app, "_run_embedding_model_probe", lambda: 0)

    assert app.main(("Amadeus", "--embedding-model-probe")) == 0


def test_fts_probe_runs_before_qt_and_returns_only_an_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(app, "_run_fts_degraded_probe", lambda: 0)

    assert app.main(("Amadeus", "--fts-degraded-probe")) == 0


def test_fts_degraded_probe_uses_an_empty_local_model_boundary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert app._run_fts_degraded_probe() == 0


def test_lifecycle_probe_observes_a_terminal_status_published_before_subscription(
    monkeypatch,
) -> None:
    callbacks = []

    class FakeTimer:
        @staticmethod
        def singleShot(*arguments) -> None:
            callbacks.append(arguments[-1])

    class FakeSignal:
        def connect(self, callback) -> None:
            self.callback = callback

    class FakeController:
        def __init__(self) -> None:
            self.vector_index = SimpleNamespace(
                status_changed=FakeSignal(),
                status=SimpleNamespace(category="model_missing"),
            )
            self.exit_requests = 0

        def request_exit(self) -> None:
            self.exit_requests += 1

    controller = FakeController()
    result = []
    monkeypatch.setattr(app, "QTimer", FakeTimer)

    app._configure_embedding_lifecycle_probe(object(), controller, "degraded", result)

    assert result == [True]
    assert len(callbacks) == 2
    callbacks[0]()
    callbacks[1]()
    assert controller.exit_requests == 1


def test_ready_lifecycle_requires_tray_builtin_pet_and_visible_pet_on_windows() -> None:
    application = SimpleNamespace(platformName=lambda: "windows")
    controller = SimpleNamespace(
        tray=SimpleNamespace(is_visible=True),
        pet_window=SimpleNamespace(
            isVisible=lambda: True,
            asset=SimpleNamespace(manifest=SimpleNamespace(pet_id="builtin-amadeus")),
        ),
    )

    assert _ready_lifecycle_surface_is_valid(application, controller)
    controller.tray.is_visible = False
    assert not _ready_lifecycle_surface_is_valid(application, controller)
    controller.tray.is_visible = True
    controller.pet_window.asset.manifest.pet_id = "private-import"
    assert not _ready_lifecycle_surface_is_valid(application, controller)


def test_offscreen_ready_lifecycle_keeps_existing_model_only_semantics() -> None:
    application = SimpleNamespace(platformName=lambda: "offscreen")

    assert _ready_lifecycle_surface_is_valid(application, SimpleNamespace())


def test_embedding_probe_requires_ready_cpu_512_model(monkeypatch) -> None:
    import amadeus_desktop.embedding_model as model

    monkeypatch.setattr(
        model,
        "resolve_runtime_model_directory",
        lambda _path: _path,
    )
    monkeypatch.setattr(
        model,
        "verify_model",
        lambda _path: ModelVerification(
            ModelAvailability.READY,
            dimension=512,
            provider="CPUExecutionProvider",
        ),
    )

    assert app._run_embedding_model_probe() == 0


def test_embedding_probe_fails_closed_for_wrong_provider(monkeypatch) -> None:
    import amadeus_desktop.embedding_model as model

    monkeypatch.setattr(model, "resolve_runtime_model_directory", lambda _path: _path)
    monkeypatch.setattr(
        model,
        "verify_model",
        lambda _path: ModelVerification(
            ModelAvailability.READY,
            dimension=512,
            provider="UnexpectedProvider",
        ),
    )

    assert app._run_embedding_model_probe() == 1
