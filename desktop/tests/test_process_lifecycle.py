from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

HELPER = Path(__file__).parent / "helpers" / "lifecycle_runner.py"


def helper_command(
    instance_name: str,
    local_app_data: Path,
    *extra_arguments: str,
) -> list[str]:
    return [
        sys.executable,
        str(HELPER),
        "--instance-name",
        instance_name,
        "--local-app-data",
        str(local_app_data),
        *extra_arguments,
    ]


def subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    return environment


def wait_for_file(path: Path, process: subprocess.Popen[str], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"Primary process exited before readiness: {process.returncode} {stdout} {stderr}"
            )
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {path}")


def test_twenty_clean_start_exit_cycles(tmp_path: Path) -> None:
    environment = subprocess_environment()

    for index in range(20):
        result = subprocess.run(
            helper_command(
                f"amadeus-cycle-{uuid4().hex}",
                tmp_path / f"cycle-{index}",
                "--auto-exit-ms",
                "40",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env=environment,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)


def test_second_process_activates_primary_and_does_not_remain(tmp_path: Path) -> None:
    environment = subprocess_environment()
    instance_name = f"amadeus-pair-{uuid4().hex}"
    ready_file = tmp_path / "ready"
    activated_file = tmp_path / "activated"
    primary = subprocess.Popen(
        helper_command(
            instance_name,
            tmp_path / "local-app-data",
            "--ready-file",
            str(ready_file),
            "--activated-file",
            str(activated_file),
            "--exit-on-activation",
            "--auto-exit-ms",
            "10000",
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        wait_for_file(ready_file, primary)
        secondary = subprocess.run(
            helper_command(instance_name, tmp_path / "local-app-data"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        assert secondary.returncode == 0, (secondary.stdout, secondary.stderr)
        assert primary.wait(timeout=10) == 0
        assert activated_file.read_text(encoding="utf-8") == "activated"
    finally:
        if primary.poll() is None:
            primary.terminate()
            primary.wait(timeout=5)
