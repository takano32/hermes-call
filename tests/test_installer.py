"""Exercise launcher discovery without apt, sudo, Hermes execution or installation."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def bash_path():
    if os.name == "nt":
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        if candidate.exists():
            return str(candidate)
    elif candidate := shutil.which("bash"):
        return candidate
    pytest.skip("Bash is required for installer discovery tests")


def posix_path(path):
    value = str(path).replace("\\", "/")
    return "/" + value[0].lower() + value[2:] if os.name == "nt" else value


@pytest.mark.parametrize(
    "location", [".local/bin/hermes", ".hermes/hermes-agent/.hermes/bin/hermes"]
)
def test_detect_installed_launcher_outside_path(tmp_path, location):
    executable = tmp_path / location
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 99\n")  # Discovery must not execute the launcher.
    executable.chmod(0o700)
    result = discover(tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == posix_path(executable)


def discover(home, explicit=None):
    env = {**os.environ, "HOME": posix_path(home), "PATH": "/usr/bin:/bin"}
    for key in ("HERMES_BIN", "HERMES_INSTALL_DIR", "HERMES_HOME", "BASH_ENV"):
        env.pop(key, None)
    if explicit is not None:
        env["HERMES_BIN"] = posix_path(explicit)
    script = Path(__file__).parents[1] / "scripts/find-hermes.sh"
    return subprocess.run(
        [bash_path(), posix_path(script)],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_explicit_invalid_path_is_not_silently_ignored(tmp_path):
    result = discover(tmp_path, tmp_path / "missing-hermes")
    assert result.returncode == 1
    assert "HERMES_BIN" in result.stderr


def test_missing_launcher_reports_detection_failure_not_setup_state(tmp_path):
    result = discover(tmp_path)
    assert result.returncode == 1
    assert "This does not mean Hermes is uninstalled" in result.stderr
