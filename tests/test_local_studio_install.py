"""Exercise the public studio harness once against an installed wheel."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def test_installed_studio_lifecycle_and_historical_duration(tmp_path: Path) -> None:
    """Require real media, restart equality and explicit uncertainty through both transports."""
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "studio-proof"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "dogfood_local_studio.py"),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    events = json.loads((output / "consumer-transcript.json").read_text())
    kinds = [event["kind"] for event in events]
    assert kinds[-1] == "studio-proof-passed"
    for expected in (
        "probe-and-independent-revisions-passed",
        "completion-waits-and-archive-passed",
        "reopen-and-historical-equality-passed",
    ):
        assert expected in kinds
    assert kinds.count("mcp-discovery") == kinds.count("mcp-closed") == 2
    assert kinds.count("report-restart-equal") == 3
    assert kinds.count("artifact-verified") == 3
    isolation = events[0]
    assert isolation["kind"] == "isolation" and isolation["python_isolated_mode"] is True
    assert isolation["source_and_cleo_reads_denied"] >= 2
    assert "site-packages" in isolation["installed_module"]
    bootstrap = json.loads((output / "bootstrap.json").read_text())
    artifact = next(event for event in bootstrap if event["kind"] == "artifact")
    assert artifact["sha256"] == hashlib.sha256(Path(artifact["wheel"]).read_bytes()).hexdigest()
    if "VIDEOGENIE_TEST_WHEEL" in env:
        assert artifact["supplied"] is True and artifact["wheel"] == env["VIDEOGENIE_TEST_WHEEL"]
    assert not (output / ".cleo").exists()
