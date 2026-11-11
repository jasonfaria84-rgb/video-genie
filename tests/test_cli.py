"""Exercise installed entry points outside the source checkout."""

import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest


@pytest.mark.parametrize("command", [("videogenie",), (sys.executable, "-m", "videogenie")])
@pytest.mark.parametrize("arguments", [(), ("--help",), ("--version",), ("unknown-command",)])
def test_installed_cli(
    command: tuple[str, ...], arguments: tuple[str, ...], tmp_path: Path
) -> None:
    """Both entry points expose help/version and reject unsupported commands."""
    result = subprocess.run(
        [*command, *arguments],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if arguments == ("unknown-command",):
        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr
        return

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    if arguments == ("--version",):
        assert result.stdout.strip() == f"videogenie {version('video-genie')}"
    else:
        assert "usage: videogenie" in result.stdout
        assert "not implemented yet" in result.stdout
