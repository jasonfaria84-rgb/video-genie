"""Acceptance proof runs a copied public consumer against one installed wheel."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import JsonValue, TypeAdapter


def test_external_cli_mcp_and_interrupted_recovery(tmp_path: Path) -> None:
    """Require both transports, real video, denied source access, and default-lease recovery."""
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "proof"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "dogfood_external_agent.py"),
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
    final = json.loads(result.stdout)
    assert final["success"] is True and final["evidence_dir"] == str(output)
    events = TypeAdapter(list[dict[str, JsonValue]]).validate_json(
        (output / "consumer-transcript.json").read_text()
    )
    kinds = [event["kind"] for event in events]
    assert kinds.count("mcp-discovery") == 2
    assert kinds.count("artifact-verified") == 2
    assert "owned-worker-interrupted" in kinds and "interruption-recovered" in kinds
    assert "negative-paths-passed" in kinds and kinds[-1] == "scenario-passed"
    isolation = next(event for event in events if event["kind"] == "isolation")
    assert isolation["python_isolated_mode"] is True
    denied = isolation["source_and_cleo_reads_denied"]
    assert isinstance(denied, int) and denied >= 2
    assert "site-packages" in str(isolation["installed_module"])
    bootstrap = json.loads((output / "bootstrap.json").read_text())
    artifact = next(event for event in bootstrap if event["kind"] == "artifact")
    assert artifact["sha256"] == hashlib.sha256(Path(artifact["wheel"]).read_bytes()).hexdigest()
    if "VIDEOGENIE_TEST_WHEEL" in env:
        assert artifact["supplied"] is True
        assert artifact["wheel"] == env["VIDEOGENIE_TEST_WHEEL"]
    assert not (output / ".cleo").exists()
