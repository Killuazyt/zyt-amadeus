from types import SimpleNamespace

from amadeus_desktop import app
from amadeus_desktop.app import (
    _acceptance_instance_name,
    _auto_exit_delay,
    _embedding_lifecycle_mode,
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
