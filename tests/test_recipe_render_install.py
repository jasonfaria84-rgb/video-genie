"""One noneditable installed proof, with synthetic owner evidence labelled separately."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_installed_recipe_render_owner_channel_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real public transports/media/recovery without source fixtures or DB writes."""
    repository = Path(__file__).resolve().parents[1]
    output = tmp_path / "recipe-proof"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    process = subprocess.Popen(
        [
            sys.executable,
            str(repository / "scripts" / "dogfood_recipe_render.py"),
            "--output-dir",
            str(output),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=420)
    except BaseException as failure:
        process.terminate()
        try:
            process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            failure.add_note("Owned bootstrap required forced termination")
        raise
    assert process.returncode == 0, stdout + stderr
    events = json.loads((output / "consumer-transcript.json").read_text())
    kinds = [event["kind"] for event in events]
    assert kinds[-1] == "recipe-proof-passed"
    assert kinds.count("mcp-discovery") == kinds.count("mcp-closed") == 2
    assert kinds.count("artifact-independent-verification") == 3
    assert "synthetic-final-review-and-selection-passed" in kinds
    assert "forged-proof-rejected" in kinds
    assert "real-interruption-recovery-passed" in kinds
    admissions = [e for e in events if e["kind"] == "scope-only-admission"]
    assert [e["stage"] for e in admissions] == ["pending", "render", "reviews", "rebound"]
    for event in admissions:
        assert set(event["admission"]) == {"state", "scope", "actor"}
        assert set(event["admission"]["scope"]) == {"project_id", "storyline_id", "client_id"}
        assert "--owner-port" not in event["argv"]
        folder = output / ("scope-" + event["stage"])
        scope_events = json.loads((folder / "transcript.json").read_text())
        assert scope_events[0]["admission"] == event["admission"]
        assert scope_events[0]["python_isolated_mode"]
        assert scope_events[-1]["kind"] == "scope-only-complete"
        assert scope_events[-1]["owner_channel_used"] is False
        assert scope_events[-1]["human_consent"] is False
        discovered = next(e for e in scope_events if e["kind"] == "mcp-discovery")
        assert len(discovered["tools"]["tools"]) == 69
        for name in ("cli-a", "mcp-b", "cli-c", "mcp-d"):
            assert not list((folder / name).iterdir())
    results = {e["stage"]: e["result"] for e in events if e["kind"] == "scope-only-result"}
    assert set(results["pending"]["pending_challenge_ids"]) <= set(
        results["render"]["completed_challenge_ids"]
    )
    assert results["rebound"]["current_policy"] > results["render"]["current_policy"]
    assert (
        results["rebound"]["render_request"]["grant_id"]
        != results["render"]["render_request"]["grant_id"]
    )
    handoff = json.loads((output / "native-handoff.json").read_text())
    assert set(handoff) == {"state", "scope", "actor"}
    auditor = json.loads((output / "auditor-expectations.json").read_text())
    assert auditor["not_for_consumer"] is True
    assert auditor["unused_grant"]["grant_id"] not in {
        results[stage]["render_request"]["grant_id"] for stage in ("render", "rebound")
    }
    identity = events[0]
    assert identity["noneditable"] and identity["python_isolated_mode"]
    assert identity["record_entries_verified"] >= len(identity["package_files"]) > 30
    bootstrap = json.loads((output / "bootstrap.json").read_text())
    artifact = next(event for event in bootstrap if event["kind"] == "artifact")
    assert artifact["sha256"] == hashlib.sha256(Path(artifact["wheel"]).read_bytes()).hexdigest()
    assert artifact["sha256"] == identity["wheel_sha256"]
    if "VIDEOGENIE_TEST_WHEEL" in env:
        assert artifact["supplied"] and artifact["wheel"] == env["VIDEOGENIE_TEST_WHEEL"]
    recovery = next(
        event for event in events if event["kind"] == "real-interruption-recovery-passed"
    )
    assert recovery["lease_was_waited"] and not recovery["database_writes_by_harness"]
    assert recovery["after"]["fence"] == 2
    duration = next(event["report"] for event in events if event["kind"] == "closed-render-cycle")
    assert duration["processing_active_seconds_known"] > 0
    assert duration["project_elapsed_end_at"] is not None
    assert events[-1]["human_owner_acceptance"] == "pending distinct physical ceremony"
    assert not list((output / "owner-private").iterdir())
    for name in ("cli-a", "mcp-b", "cli-c", "mcp-d"):
        assert not list((output / name).iterdir())
    assert not (output / ".cleo").exists()

    # A real console remains owned by bootstrap when its separate consumer times out.
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    from dogfood_external_agent import Transcript, consumer_environment
    from dogfood_recipe_render import OwnedConsole, run_consumer

    fault_root = tmp_path / "consumer-timeout"
    fault_root.mkdir()
    runtime = output / "runtime"
    fault_env = consumer_environment(fault_root, runtime)
    console = OwnedConsole(fault_root, runtime, fault_env)
    with pytest.raises(subprocess.TimeoutExpired):
        run_consumer(
            console,
            Transcript(fault_root),
            [str(runtime / "bin" / "python"), "-I", "-c", "import time; time.sleep(10)"],
            cwd=fault_root,
            env=fault_env,
            timeout=0.05,
        )
    assert console.child is not None and console.child.poll() == 0
    assert not console.handoff.exists()
    assert console.master is None
