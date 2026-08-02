"""Explicit preparation and verification CLI for the pinned offline model."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from amadeus_desktop.embedding_model import (
    MODEL_API_NAME,
    MODEL_REVISION,
    ModelAvailability,
    ModelPreparationError,
    ModelVerification,
    model_directory,
    prepare_model,
    repair_model,
    verify_model,
)
from amadeus_desktop.paths import AppDirectory, AppPaths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="amadeus-model",
        description="Prepare or verify the fixed P5B offline embedding model.",
    )
    parser.add_argument("operation", choices=("prepare", "verify", "repair"))
    parser.add_argument(
        "--models-root",
        type=Path,
        help="Override the Amadeus models directory (primarily for isolated acceptance).",
    )
    parser.add_argument(
        "--path",
        type=Path,
        help="Verify one prepared revision directory without changing it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    models_root = args.models_root or AppPaths.for_current_user().directory(AppDirectory.MODELS)
    try:
        if args.path is not None and args.operation != "verify":
            raise ModelPreparationError("model_path_not_allowed")
        if args.operation == "prepare":
            result = prepare_model(models_root)
        elif args.operation == "repair":
            result = repair_model(models_root)
        else:
            result = verify_model(args.path or model_directory(models_root))
    except ModelPreparationError as error:
        _print_result(
            args.operation,
            ModelVerification(
                availability=ModelAvailability.RUNTIME_UNAVAILABLE,
                error_category=str(error),
            ),
        )
        return 1
    except Exception:
        _print_result(
            args.operation,
            ModelVerification(
                availability=ModelAvailability.RUNTIME_UNAVAILABLE,
                error_category="model_operation_failed",
            ),
        )
        return 1

    _print_result(args.operation, result)
    return 0 if result.ready else 1


def _print_result(operation: str, result: ModelVerification) -> None:
    # Deliberately omit local paths and exception text. This payload is safe for
    # diagnostics and acceptance evidence.
    print(
        json.dumps(
            {
                "operation": operation,
                "status": str(result.availability),
                "error_category": result.error_category,
                "model": MODEL_API_NAME,
                "revision": MODEL_REVISION,
                "dimension": result.dimension,
                "provider": result.provider,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
