"""Installed public recipe proof with isolated synthetic WebAuthn, never human consent."""

import argparse
import asyncio
import base64
import csv
import fcntl
import hashlib
import io
import json
import os
import pty
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import time
import zipfile
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from dogfood_external_agent import Consumer, Transcript, consumer_environment, object_value, string
from PIL import Image
from pydantic import JsonValue


def data(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Read transport data without importing an application service."""
    return object_value(value["data"])


def encoded(value: bytes) -> str:
    """Encode WebAuthn wire bytes without padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def sha(value: bytes) -> str:
    """Hash exact observed bytes."""
    return hashlib.sha256(value).hexdigest()


def canonical(value: JsonValue) -> str:
    """Digest public JSON data independently of request-envelope IDs."""
    return sha(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode()
    )


class OwnedConsole:
    """Bootstrap owns this direct child even if the separate consumer times out."""

    def __init__(self, root: Path, runtime: Path, env: dict[str, str]) -> None:
        self.env = env
        self.cli_path = runtime / "bin" / "videogenie"
        self.state = root / "state"
        self.cwd = root / "owner-launch"
        self.cwd.mkdir()
        private = root / "owner-private"
        private.mkdir(mode=0o700)
        self.handoff = private / "launch.txt"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.master: int | None = None
        self.child: subprocess.Popen[str] | None = None

    def start(self) -> None:
        """Admit only disposable enrollment via the existing controlling-TTY command."""
        master, slave = pty.openpty()
        self.master = master

        def terminal() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        argv = [
            str(self.cli_path),
            "--data-dir",
            str(self.state),
            "owner",
            "enroll",
            "--port",
            str(self.port),
            "--handoff-file",
            str(self.handoff),
        ]
        self.child = subprocess.Popen(
            argv,
            cwd=self.cwd,
            env=self.env,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=terminal,
        )
        os.close(slave)
        assert select.select([master], [], [], 15)[0], "No enrollment supervision prompt"
        prompt = os.read(master, 4096)
        assert b"ENROLL" in prompt
        os.write(master, b"ENROLL\n")
        assert self.child.stdout is not None
        assert select.select([self.child.stdout], [], [], 15)[0], "No owner launch result"
        ready = object_value(json.loads(self.child.stdout.readline()))
        assert ready["success"] is True

    def close(self) -> None:
        """Stop only the owned console and drain its synthetic terminal without logging secrets."""
        try:
            if self.child is not None:
                if self.child.poll() is None:
                    self.child.send_signal(signal.SIGTERM)
                deadline = time.monotonic() + 10
                while self.child.poll() is None and time.monotonic() < deadline:
                    if self.master is not None and select.select([self.master], [], [], 0.05)[0]:
                        try:
                            os.read(self.master, 4096)
                        except OSError:
                            break
                try:
                    out, err = self.child.communicate(timeout=5)
                except subprocess.TimeoutExpired as failure:
                    self.child.kill()
                    self.child.communicate(timeout=5)
                    failure.add_note(
                        "Owned console required forced termination; handoff may remain"
                    )
                    raise
                assert self.child.returncode == 0 and out == "" and err == "", (out, err)
        finally:
            if self.master is not None:
                os.close(self.master)
                self.master = None
        assert not self.handoff.exists()


class SyntheticOwner:
    """Drive the installed supervised HTTP channel with an explicitly synthetic credential.

    This exercises real verifier signatures and transactions. It is neither a
    native browser authenticator nor evidence of a physical human's consent.
    """

    def __init__(self, consumer: Consumer, port: int) -> None:
        self.consumer = consumer
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential = uuid4().bytes
        self.count = 0
        self.rejected_signature = False
        self.cookie = ""
        self.csrf = ""
        self.port = port
        self.origin = f"http://localhost:{port}"
        self.handoff = consumer.root / "owner-private" / "launch.txt"

    def start(self) -> None:
        """Consume the supervised parent's one-use launch; never import owner services."""
        assert stat.S_IMODE(self.handoff.stat().st_mode) == 0o600
        url = self.handoff.read_text().splitlines()[1]
        token = parse_qs(urlsplit(url).fragment)["launch"][0]
        status, headers, raw = self.http("/session", {}, launch=token)
        assert status == 200
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = string(object_value(json.loads(raw)), "csrf")
        assert self.http("/session", {}, launch=token)[0] == 403
        options = self.post("/enrollment/options", {}, retain=False)
        challenge = object_value(options["challenge"])
        point = self.key.public_key().public_numbers()
        public = cbor2.dumps(
            {1: 2, 3: -7, -1: 1, -2: point.x.to_bytes(32), -3: point.y.to_bytes(32)}
        )
        client = json.dumps(
            {"type": "webauthn.create", "origin": self.origin, "challenge": challenge["nonce"]}
        ).encode()
        authenticator = (
            hashlib.sha256(b"localhost").digest()
            + bytes([69])
            + bytes(4)
            + bytes(16)
            + len(self.credential).to_bytes(2)
            + self.credential
            + public
        )
        receipt = self.post(
            "/enrollment/complete",
            {
                "application_instance_id": challenge["application_instance_id"],
                "challenge_id": challenge["challenge_id"],
                "credential_id": encoded(self.credential),
                "client_data_json": encoded(client),
                "attestation_object": encoded(
                    cbor2.dumps({"fmt": "none", "authData": authenticator, "attStmt": {}})
                ),
            },
        )
        assert data(receipt)["user_verified"] is True
        self.consumer.log.record(
            "synthetic-owner-enrolled", origin=self.origin, receipt=receipt, human_consent=False
        )

    def http(
        self, route: str, payload: dict[str, JsonValue] | None = None, *, launch: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        """Use ordinary HTTP; session tokens and offline recovery bytes never enter logs."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=90)
        headers = {"Host": f"localhost:{self.port}", "Origin": self.origin}
        if self.cookie:
            headers.update({"Cookie": self.cookie, "X-CSRF-Token": self.csrf})
        if launch:
            headers["X-Console-Launch"] = launch
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(
                "GET" if payload is None else "POST",
                route,
                None if payload is None else json.dumps(payload),
                headers,
            )
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post(
        self, route: str, payload: dict[str, JsonValue], *, retain: bool = True
    ) -> dict[str, JsonValue]:
        status, _, raw = self.http(route, payload)
        value = object_value(json.loads(raw))
        if retain:
            self.consumer.log.record(
                "owner-http",
                route=route,
                status=status,
                response=value,
                response_sha256=sha(raw),
                synthetic=True,
            )
        assert status == 200, (route, status, value)
        return value

    def approve(
        self, challenge: dict[str, JsonValue], *, preview: dict[str, JsonValue] | None = None
    ) -> dict[str, JsonValue]:
        """Review exact public challenge, then submit a genuinely verified synthetic signature."""
        identity = {key: challenge[key] for key in ("application_instance_id", "challenge_id")}
        self.post("/action/review", identity)
        self.post("/action/select", identity)
        if preview is not None:
            self.post("/preview/create", preview)
        client = json.dumps(
            {"type": "webauthn.get", "origin": self.origin, "challenge": challenge["nonce"]}
        ).encode()
        self.count += 1
        auth = hashlib.sha256(b"localhost").digest() + bytes([5]) + self.count.to_bytes(4)
        signature = self.key.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        proof = identity | {
            "assertion": {
                "credential_id": encoded(self.credential),
                "client_data_json": encoded(client),
                "authenticator_data": encoded(auth),
                "signature": encoded(signature),
            }
        }
        if not self.rejected_signature:
            invalid = proof | {"assertion": object_value(proof["assertion"]) | {"signature": "AA"}}
            status, _, raw = self.http("/action/apply", invalid)
            assert status in (400, 403)
            self.consumer.log.record(
                "forged-proof-rejected", status=status, response=object_value(json.loads(raw))
            )
            self.rejected_signature = True
        result = self.post("/action/apply", proof)
        assert self.post("/action/apply", proof) == result
        return data(result)


class RecipeConsumer(Consumer):
    """Public agents prepare content and execute only independently granted local operations."""

    def request_action(
        self,
        owner: SyntheticOwner,
        action: dict[str, JsonValue],
        key: str,
        preview: dict[str, JsonValue] | None = None,
    ) -> dict[str, JsonValue]:
        challenge = data(self.cli("approval.request", {"action": action, "idempotency_key": key}))
        return owner.approve(challenge, preview=preview)

    def scope_process(self, scope: dict[str, JsonValue], stage: str) -> dict[str, JsonValue]:
        """Start a fresh isolated consumer without inherited domain IDs or owner channel."""
        folder = self.root / ("scope-" + stage)
        folder.mkdir()
        admission: dict[str, JsonValue] = {
            "state": str(self.state),
            "scope": scope,
            "actor": "fictional-agent-" + stage,
        }
        argv = [
            str(self.runtime / "bin" / "python"),
            "-I",
            "-c",
            "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
            "sys.argv=[sys.argv[1]+'/dogfood_recipe_render.py',*sys.argv[2:]];"
            "runpy.run_path(sys.argv[0],run_name='__main__')",
            str(self.root),
            "--scope-stage",
            stage,
        ]
        self.log.record(
            "scope-only-admission", stage=stage, admission=admission, argv=[part for part in argv]
        )
        child = subprocess.Popen(
            argv,
            cwd=folder,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            out, err = child.communicate(json.dumps(admission), timeout=150)
        except BaseException as failure:
            child.terminate()
            try:
                child.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)
                failure.add_note("Owned scope consumer required forced termination")
            raise
        self.log.record(
            "scope-only-exit", stage=stage, exit_code=child.returncode, stdout=out, stderr=err
        )
        assert child.returncode == 0, (out, err)
        result = object_value(json.loads(out))
        self.log.record(
            "scope-only-result",
            stage=stage,
            result=result,
            transcript_sha256=sha((folder / "transcript.json").read_bytes()),
        )
        return result

    async def recipe_scenario(self, owner: SyntheticOwner) -> None:
        """Prepare exact content and prove real rendering/replay through separate public clients."""
        capabilities = data(self.cli("capabilities", {}))
        operations = capabilities["operations"]
        assert isinstance(operations, list) and len(operations) == 79
        assert capabilities["owner_mutations"] == "console_only"
        fixture = self.root / "fixture"
        lock = object_value(json.loads((fixture / "fonts.lock.json").read_text()))
        license_record = object_value(lock["license"])
        assert (
            sha((fixture / "Inter-Bold.ttf").read_bytes()) == object_value(lock["font"])["sha256"]
        )
        provenance = {
            key: license_record[key]
            for key in ("license_id", "source_url", "text_sha256", "asserted_at")
        }
        assert sha((fixture / "OFL.txt").read_bytes()) == provenance["text_sha256"]
        asset = data(
            self.cli(
                "asset.register",
                {
                    "source_path": str(fixture / "Inter-Bold.ttf"),
                    "storage_mode": "copy",
                    "media_type": "font/ttf",
                    "origin": {
                        "kind": "curated",
                        "source_url": object_value(lock["source"])["release_url"],
                    },
                    "license_provenance": provenance,
                    "max_bytes": 500000,
                    "idempotency_key": "font",
                },
            )
        )
        pin = {key: asset[key] for key in ("asset_id", "version_id", "sha256")}
        client = data(
            self.cli(
                "client.create", {"name": "Fictional Garden Studio", "idempotency_key": "client"}
            )
        )
        project = data(
            self.cli(
                "project.create",
                {"name": "Fictional garden volunteer", "idempotency_key": "project"},
            )
        )
        scope = {key: project[key] for key in ("project_id", "storyline_id")}
        scope["client_id"] = client["client_id"]
        link = data(
            self.cli(
                "project.client.assign",
                {
                    "project_id": scope["project_id"],
                    "client_id": scope["client_id"],
                    "expected_project_revision": 1,
                },
            )
        )
        revision = link["project_revision"]
        brand = data(
            self.cli(
                "brand.draft.create",
                {
                    "client_id": client["client_id"],
                    "content": {
                        "name_font": pin,
                        "role_font": pin,
                        "name_font_pixels": 42,
                        "role_font_pixels": 22,
                        "panel_color": "#18283DE6",
                        "name_color": "#FFFFFFFF",
                        "role_color": "#D5E5FFFF",
                    },
                    "idempotency_key": "brand",
                },
            )
        )
        recipe = data(
            self.cli(
                "recipe.draft.create",
                {
                    "content": {
                        "resources": {"timeout_seconds": 30, "max_artifact_bytes": 268435456}
                    },
                    "idempotency_key": "recipe",
                },
            )
        )
        preview: dict[str, JsonValue] = {
            "recipe": {
                key: recipe[key] for key in ("recipe_id", "draft_revision", "content_sha256")
            },
            "brand": {
                key: brand[key]
                for key in ("client_id", "brand_id", "draft_revision", "content_sha256")
            },
            "fixture": {},
            "idempotency_key": "publication-preview",
        }
        pending = data(
            self.cli(
                "approval.request",
                {
                    "action": {
                        "kind": "publish_recipe",
                        "recipe_id": recipe["recipe_id"],
                        "expected_draft_revision": recipe["draft_revision"],
                        "content_sha256": recipe["content_sha256"],
                    },
                    "idempotency_key": "lost-pending-recipe",
                },
            )
        )
        pending_found = self.scope_process(scope, "pending")
        pending_ids = pending_found["pending_challenge_ids"]
        assert isinstance(pending_ids, list) and pending["challenge_id"] in pending_ids
        recipe_version = owner.approve(pending, preview=preview)
        brand_version = self.request_action(
            owner,
            {
                "kind": "publish_brand",
                "brand_id": brand["brand_id"],
                "client_id": client["client_id"],
                "expected_draft_revision": brand["draft_revision"],
                "content_sha256": brand["content_sha256"],
            },
            "publish-brand",
            preview,
        )
        recipe_pin = {
            key: recipe_version[key] for key in ("recipe_id", "version_id", "content_sha256")
        }
        brand_pin = {
            key: brand_version[key] for key in ("brand_id", "version_id", "content_sha256")
        }
        binding: dict[str, JsonValue] = scope | {
            "recipe": recipe_pin,
            "brand": brand_pin,
            "parameters": {"name": "JOSÉ NÚÑEZ-ØSTBY", "role": "COMMUNITY GARDEN VOLUNTEER"},
            "config_id": None,
            "expected_config_revision": 0,
            "expected_project_revision": revision,
            "idempotency_key": "binding",
        }
        config = data(self.cli("project.recipe.bind", binding))
        observed_binding = data(self.cli("project.recipe.context", scope))
        policy = object_value(observed_binding["scope"])["current_policy_revision"]
        now = datetime.now(UTC)
        terms: dict[str, JsonValue] = scope | {
            "recipe": recipe_pin,
            "brand": brand_pin,
            "config_policy_revision": policy,
            "allowed_capabilities": ["graphics.render_lower_third"],
            "allowed_providers": ["local-pillow-ffmpeg"],
            "permitted_input_classes": ["pinned_brand_assets"],
            "parameter_bounds": {"name_max_length": 60, "role_max_length": 80},
            "resource_bounds": object_value(recipe["content"])["resources"],
            "max_runs": 1,
            "max_attempts_per_run": 2,
            "max_concurrent": 1,
            "valid_from": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "monetary_caps_by_currency": [
                {"minor_units": 0, "currency": {"code": "USD", "exponent": 2}}
            ],
        }
        grant = self.request_action(
            owner,
            {"kind": "issue_grant", "terms": terms, "expected_project_revision": revision},
            "grant",
        )
        opened = data(
            self.cli(
                "project.cycle.open",
                {"project_id": scope["project_id"], "expected_activity_revision": 0},
            )
        )
        render: dict[str, JsonValue] = scope | {
            "config_id": config["config_id"],
            "config_revision": config["config_revision"],
            "config_sha256": config["config_sha256"],
            "expected_project_revision": revision,
            "grant_id": grant["grant_id"],
            "idempotency_key": "first-render",
        }
        continued = self.scope_process(scope, "render")
        render = object_value(continued["render_request"])
        assert render["grant_id"] == grant["grant_id"]
        completed_ids = continued["completed_challenge_ids"]
        assert isinstance(completed_ids, list) and pending["challenge_id"] in completed_ids
        assert object_value(continued["job"])["status"] == "succeeded"
        self.log.record(
            "prepared-public-render",
            scope=scope,
            config=config,
            grant=grant,
            request=render,
            human_consent=False,
        )
        async with self.connect("mcp-b") as session:
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "videogenie_" + str(op).replace(".", "_") for op in operations
            }
            assert data(await self.mcp(session, "capabilities", {})) == capabilities
            job = data(await self.mcp(session, "graphics.render_lower_third", render))
            assert job["status"] == "succeeded", job
            assert data(self.cli("graphics.render_lower_third", render)) == job
            reference = object_value(job["result_ref"])
            lookup = scope | {"artifact_id": reference["artifact_id"]}
            artifact = data(self.cli("artifact.get", lookup))
            assert data(await self.mcp(session, "artifact.get", lookup)) == artifact
            self.verify_artifact(artifact)
            self.log.record("real-render-and-replay-passed", job=job, artifact=artifact)
            denied = await self.mcp(
                session,
                "graphics.render_lower_third",
                render | {"idempotency_key": "over-budget"},
                success=False,
            )
            assert object_value(object_value(denied["error"])["details"])["reason"] == "run_limit"
        cost = data(self.cli("cost.report", {"project_id": scope["project_id"]}))
        cost_request = {"project_id": scope["project_id"]} | object_value(cost["snapshot"])
        cost_request.pop("schema_version")
        currencies = cost["currencies"]
        assert isinstance(currencies, list) and len(currencies) == 1
        money = object_value(currencies[0])
        assert money["actual_provider_minor"] == money["net_actual_minor"] == 0
        assert money["reserved_exposure_minor"] == 0
        assert all(
            money[key] is None for key in ("client_charge", "internal_cost", "total_economic_cost")
        )
        quantities = cost["unpriced_quantities"]
        assert isinstance(quantities, list) and quantities
        assert all(object_value(item)["unit"] == "worker_wall_seconds" for item in quantities)
        view = owner.post("/output/review", lookup)
        target = object_value(view["target"])
        evidence = object_value(view["evidence"])
        assert evidence["manifest"] == artifact["manifest"]
        preview_request = object_value(evidence["request"])
        assert all(preview_request[key] == value for key, value in scope.items())
        assert all(
            preview_request[key] == reference[key]
            for key in ("job_id", "artifact_id", "manifest_sha256")
        )
        assert preview_request["artifact_sha256"] == reference["sha256"]
        master_frames = object_value(artifact["manifest"])["decoded_frame_sha256"]
        assert isinstance(master_frames, list)
        samples = evidence["keyframes"]
        assert isinstance(samples, list) and len(samples) == 5
        assert [object_value(sample)["frame_index"] for sample in samples] == [0, 16, 75, 129, 149]
        for item in samples:
            sample = object_value(item)
            frame_index = sample["frame_index"]
            assert isinstance(frame_index, int) and not isinstance(frame_index, bool)
            assert sample["rgba_sha256"] == master_frames[frame_index]
            status, _, raw = owner.http(
                string(view, "media_base") + "/" + str(sample["frame_index"])
            )
            assert status == 200 and sha(raw) == sample["png_sha256"]
            assert len(raw) == sample["png_byte_size"]
            with Image.open(io.BytesIO(raw)) as image:
                assert image.mode == "RGBA" and image.size == (1920, 1080)
                assert sha(image.tobytes()) == sample["rgba_sha256"]
        status, _, master = owner.http(string(view, "media_base") + "/video")
        assert status == 200 and sha(master) == reference["sha256"]
        action: dict[str, JsonValue] = {
            "kind": "review_output",
            "target": target,
            "decision": "approved",
            "comments": "Synthetic fixture only",
        }
        approval = self.request_action(owner, action, "approve-output")
        discovered_review = self.scope_process(scope, "reviews")
        approval = object_value(discovered_review["latest_review"])
        selected_request = scope | {
            "artifact_id": reference["artifact_id"],
            "artifact_sha256": reference["sha256"],
            "review_id": approval["review_id"],
            "expected_selection_revision": 0,
            "idempotency_key": "select",
        }
        selected = data(self.cli("output.select", selected_request))
        assert selected["revision"] == 1
        changes = self.request_action(owner, action | {"decision": "changes_requested"}, "changes")
        assert changes["decision"] == "changes_requested"
        rejected = self.cli(
            "output.select",
            selected_request
            | {"expected_selection_revision": 1, "idempotency_key": "superseded-approval"},
            expected=5,
        )
        assert object_value(rejected["error"])["code"] == "REVISION_CONFLICT"
        assert data(self.cli("output.select", selected_request)) == selected
        assert data(self.cli("cost.report", cost_request)) == cost
        assert data(self.cli("project.activity.get", {"project_id": scope["project_id"]})) == opened
        self.log.record(
            "synthetic-final-review-and-selection-passed",
            approved=approval,
            changes_requested=changes,
            selection=selected,
            evidence=evidence,
            cost_snapshot=cost,
            cost_sha256=canonical(cost),
            human_consent=False,
        )
        # A second explicit grant permits the recovery experiment without weakening
        # the first grant's observed one-run ceiling.
        recovery_grant = self.request_action(
            owner,
            {
                "kind": "issue_grant",
                "terms": terms | {"max_runs": 2},
                "expected_project_revision": revision,
            },
            "recovery-grant",
        )
        recovered = self.interruption(
            scope,
            render
            | {"grant_id": recovery_grant["grant_id"], "idempotency_key": "interrupted-render"},
        )
        self.verify_artifact(
            data(
                self.cli(
                    "artifact.get",
                    scope | {"artifact_id": object_value(recovered["result_ref"])["artifact_id"]},
                )
            )
        )
        self.request_action(
            owner,
            scope
            | {
                "kind": "revoke_grant",
                "grant_id": recovery_grant["grant_id"],
                "expected_grant_revision": recovery_grant["revision"],
            },
            "revoke-current-grant",
        )
        revoked = self.cli(
            "graphics.render_lower_third",
            render | {"grant_id": recovery_grant["grant_id"], "idempotency_key": "new-revoked-run"},
            expected=4,
        )
        assert object_value(revoked["error"])["code"] == "ACCESS_DENIED"
        assert object_value(object_value(revoked["error"])["details"])["reason"] == "grant_revoked"
        assert (
            data(self.cli("render.job.resume", scope | {"job_id": recovered["job_id"]}))
            == recovered
        )
        closed = data(
            self.cli(
                "project.cycle.close",
                {
                    "project_id": scope["project_id"],
                    "cycle_id": object_value(opened["current_cycle"])["cycle_id"],
                    "expected_activity_revision": opened["revision"],
                    "outcome": "completed",
                },
            )
        )
        at = datetime.now(UTC).isoformat()
        duration_request = {"project_id": scope["project_id"], "as_of": at}
        duration = data(self.cli("project.duration", duration_request))
        active = duration["processing_active_seconds_known"]
        assert isinstance(active, (float, int)) and active > 0
        self.log.record(
            "closed-render-cycle", activity=closed, report=duration, data_sha256=canonical(duration)
        )
        archived = data(
            self.cli(
                "project.archive",
                {"project_id": scope["project_id"], "expected_revision": revision},
            )
        )
        async with self.connect("mcp-d") as restarted:
            assert data(await self.mcp(restarted, "project.duration", duration_request)) == duration
            later = data(
                await self.mcp(
                    restarted,
                    "project.duration",
                    {
                        "project_id": scope["project_id"],
                        "as_of": datetime.now(UTC).isoformat(),
                    },
                )
            )
            for key in ("project_elapsed_end_at", "project_elapsed_seconds", "cycles"):
                assert later[key] == duration[key], key
            later_age, prior_age = later["project_age_seconds"], duration["project_age_seconds"]
            assert isinstance(later_age, (int, float)) and isinstance(prior_age, (int, float))
            assert later_age > prior_age
            self.log.record("completion-remains-frozen", original=duration, later=later)
            assert data(await self.mcp(restarted, "cost.report", cost_request)) == cost
            assert data(await self.mcp(restarted, "artifact.get", lookup)) == artifact
            assert (
                data(await self.mcp(restarted, "render.job.get", scope | {"job_id": job["job_id"]}))
                == job
            )
            assert data(await self.mcp(restarted, "output.select", selected_request)) == selected
        restored = data(
            self.cli(
                "project.restore",
                {"project_id": scope["project_id"], "expected_revision": archived["revision"]},
            )
        )
        new_binding = binding | {
            "config_id": config["config_id"],
            "expected_config_revision": config["config_revision"],
            "expected_project_revision": restored["revision"],
            "idempotency_key": "rebind",
        }
        current_config = data(self.cli("project.recipe.bind", new_binding))
        stale = self.cli(
            "graphics.render_lower_third",
            render
            | {
                "expected_project_revision": restored["revision"],
                "idempotency_key": "stale-binding",
            },
            expected=5,
        )
        assert object_value(stale["error"])["code"] == "REVISION_CONFLICT"
        assert object_value(object_value(stale["error"])["details"])["reason"] == "stale_binding"
        self.request_action(
            owner,
            scope
            | {
                "kind": "revoke_grant",
                "grant_id": grant["grant_id"],
                "expected_grant_revision": grant["revision"],
            },
            "revoke",
        )
        assert data(self.cli("graphics.render_lower_third", render)) == job
        rebound_context = data(self.cli("project.recipe.context", scope))
        current_policy = object_value(rebound_context["scope"])["current_policy_revision"]
        self.request_action(
            owner,
            {
                "kind": "issue_grant",
                "terms": terms | {"config_policy_revision": current_policy, "max_runs": 1},
                "expected_project_revision": restored["revision"],
            },
            "continuation-grant",
        )
        rebound = self.scope_process(scope, "rebound")
        rebound_request = object_value(rebound["render_request"])
        assert rebound_request["config_revision"] == current_config["config_revision"]
        self.verify_artifact(object_value(rebound["artifact"]))
        rebound_grant = data(
            self.cli("grant.get", scope | {"grant_id": rebound_request["grant_id"]})
        )
        self.request_action(
            owner,
            scope
            | {
                "kind": "revoke_grant",
                "grant_id": rebound_grant["grant_id"],
                "expected_grant_revision": rebound_grant["revision"],
            },
            "retire-continuation-grant",
        )
        native_grant = self.request_action(
            owner,
            {
                "kind": "issue_grant",
                "terms": terms | {"config_policy_revision": current_policy, "max_runs": 4},
                "expected_project_revision": restored["revision"],
            },
            "native-grant",
        )
        native_request = render | {
            "config_revision": current_config["config_revision"],
            "config_sha256": current_config["config_sha256"],
            "expected_project_revision": restored["revision"],
            "grant_id": native_grant["grant_id"],
            "idempotency_key": "native-first-render",
        }
        handoff: dict[str, JsonValue] = {
            "state": str(self.state),
            "scope": scope,
            "actor": "native-external-agent",
        }
        auditor: dict[str, JsonValue] = {
            "render_request": native_request,
            "activity": closed,
            "duration_request": duration_request,
            "duration_data": duration,
            "cost_request": cost_request,
            "cost_data": cost,
            "artifact_request": lookup,
            "artifact_data": artifact,
            "unused_grant": native_grant,
            "owner_provenance": "synthetic test credential, not human consent",
            "not_for_consumer": True,
        }
        (self.root / "native-handoff.json").write_text(json.dumps(handoff, indent=2) + "\n")
        (self.root / "auditor-expectations.json").write_text(json.dumps(auditor, indent=2) + "\n")
        self.log.record(
            "recipe-proof-passed",
            scope=scope,
            duration_sha256=canonical(duration),
            frozen_cost_sha256=canonical(cost),
            native_handoff="native-handoff.json",
            human_owner_acceptance="pending distinct physical ceremony",
        )

    def interruption(
        self, scope: dict[str, JsonValue], request: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        """Kill only the owned coordinator after observing its actual fixed worker child.

        No lease/database edits: wait the real advertised lease and let the public
        resume operation reconcile completion before creating a new fenced attempt.
        """
        argv = [
            str(self.cli_path),
            "--data-dir",
            str(self.state),
            "graphics",
            "render_lower_third",
            "--json",
            "--input-json",
            "-",
        ]
        coordinator = subprocess.Popen(
            argv,
            cwd=self.root / "cli-c",
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert coordinator.stdin is not None
        coordinator.stdin.write(json.dumps(request))
        coordinator.stdin.close()
        coordinator.stdin = None
        deadline = time.monotonic() + 20
        observed_child = False
        try:
            while coordinator.poll() is None and time.monotonic() < deadline:
                listing = subprocess.run(
                    ["ps", "-axo", "pid=,ppid=,command="],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                for line in listing.stdout.splitlines():
                    fields = line.strip().split(None, 2)
                    if (
                        len(fields) == 3
                        and fields[1] == str(coordinator.pid)
                        and "_render_child" in fields[2]
                    ):
                        observed_child = True
                        break
                if observed_child:
                    break
                time.sleep(0.02)
            assert observed_child, "Fixed worker did not start before owned coordinator finished"
            coordinator.kill()
            out, err = coordinator.communicate(timeout=10)
            assert coordinator.returncode == -signal.SIGKILL
            self.log.record(
                "owned-coordinator-interrupted",
                pid=coordinator.pid,
                exit_code=coordinator.returncode,
                stdout=out,
                stderr=err,
                fixed_child_observed=True,
            )
        finally:
            if coordinator.poll() is None:
                coordinator.kill()
                coordinator.communicate(timeout=10)
        listed = data(self.cli("render.job.list", scope))["jobs"]
        assert isinstance(listed, list)
        job = next(
            object_value(x)
            for x in listed
            if object_value(object_value(x)["request"])["idempotency_key"]
            == request["idempotency_key"]
        )
        held = self.cli("graphics.render_lower_third", request, expected=5)
        assert object_value(held["error"])["code"] == "LEASE_HELD"
        expiry = datetime.fromisoformat(string(job, "lease_expires_at"))
        while datetime.now(UTC) <= expiry + timedelta(milliseconds=100):
            time.sleep(min(1, max(0.01, (expiry - datetime.now(UTC)).total_seconds())))
        selected = scope | {"job_id": job["job_id"]}
        result = data(self.cli("render.job.resume", selected))
        assert result["status"] == "succeeded" and result["fence"] == 2
        attempts = result["attempts"]
        assert isinstance(attempts, list) and len(attempts) == 2
        assert object_value(attempts[0])["status"] == "interrupted"
        assert object_value(attempts[1])["status"] == "succeeded"
        assert data(self.cli("render.job.resume", selected)) == result
        self.log.record(
            "real-interruption-recovery-passed",
            before=job,
            after=result,
            lease_was_waited=True,
            database_writes_by_harness=False,
        )
        return result

    def verify_artifact(self, result: dict[str, JsonValue]) -> None:
        """Independently inspect the actual150-frame MOV and every decoded RGBA frame hash."""
        artifact = object_value(result["artifact"])
        manifest = object_value(result["manifest"])
        assert canonical(manifest) == artifact["manifest_sha256"]
        path = self.state / string(artifact, "locator")
        assert path.resolve().is_relative_to(self.state.resolve())
        raw = path.read_bytes()
        assert sha(raw) == artifact["sha256"] and len(raw) == artifact["byte_size"]
        probe = self.log.command(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            cwd=self.root,
            env=self.env,
        )
        observed = object_value(json.loads(probe.stdout))
        streams = observed["streams"]
        assert isinstance(streams, list) and len(streams) == 1
        video = object_value(streams[0])
        assert (
            video["codec_name"],
            video["pix_fmt"],
            video["width"],
            video["height"],
            video["r_frame_rate"],
            video["nb_frames"],
        ) == ("qtrle", "argb", 1920, 1080, "24/1", "150")
        command = [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgba",
            "pipe:1",
        ]
        child = subprocess.Popen(
            command, cwd=self.root, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert child.stdout is not None
        frames: list[JsonValue] = []
        deadline = time.monotonic() + 120
        try:
            for index in range(150):
                buffer = bytearray()
                while len(buffer) < 1920 * 1080 * 4:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "Independent decoder exceeded its bounded deadline"
                    assert select.select([child.stdout], [], [], remaining)[0]
                    block = os.read(child.stdout.fileno(), 1920 * 1080 * 4 - len(buffer))
                    assert block, "Independent decoder ended before frame150"
                    buffer.extend(block)
                frame = bytes(buffer)
                assert len(frame) == 1920 * 1080 * 4
                if index in (0, 149):
                    assert not any(frame[3::4]), "Endpoint is not transparent"
                frames.append(sha(frame))
            assert select.select([child.stdout], [], [], max(0, deadline - time.monotonic()))[0]
            assert os.read(child.stdout.fileno(), 1) == b""
            _, err = child.communicate(timeout=30)
            assert child.returncode == 0, err
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        assert frames == manifest["decoded_frame_sha256"]
        self.log.record(
            "artifact-independent-verification",
            artifact=artifact,
            ffprobe=observed,
            decoded_frame_sha256=frames,
            manifest_sha256=canonical(manifest),
        )


class ScopeConsumer(Consumer):
    """Fresh test client whose only domain input is the explicit public scope."""

    def __init__(self, admission: dict[str, JsonValue]) -> None:
        assert set(admission) == {"state", "scope", "actor"}
        self.root = Path.cwd()
        self.log = Transcript(self.root)
        self.runtime = Path(sys.executable).parent.parent
        self.state = Path(string(admission, "state"))
        assert self.state.is_absolute()
        self.env = consumer_environment(self.root, self.runtime)
        self.cli_path = self.runtime / "bin" / "videogenie"
        self.server_path = self.runtime / "bin" / "videogenie-mcp"
        self.scope = object_value(admission["scope"])
        assert set(self.scope) == {"project_id", "storyline_id", "client_id"}
        for name in ("cli-a", "mcp-b", "cli-c", "mcp-d"):
            (self.root / name).mkdir()
        self.log.record(
            "scope-only-input",
            admission=admission,
            actor_is_authority=False,
            python_isolated_mode=bool(sys.flags.isolated),
        )

    def pages(self, operation: str, request: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
        """Retain live coverage for every bounded page, without claiming cross-page isolation."""
        result: list[dict[str, JsonValue]] = []
        cursor: JsonValue = None
        for _ in range(20):
            page = data(self.cli(operation, request | {"limit": 1, "after": cursor}))
            rows = page["items"]
            assert isinstance(rows, list) and page["returned_count"] == len(rows)
            assert page["consistency"] == "per_response_live"
            assert page["range_exhausted"] is (not page["has_more"])
            result.extend(object_value(row) for row in rows)
            if not page["has_more"]:
                assert page["next_cursor"] is None
                return result
            assert page["next_cursor"] is not None and page["next_cursor"] != cursor
            cursor = page["next_cursor"]
        raise AssertionError("Fictional fixture exceeded bounded live inventory")


def without_observation(value: JsonValue) -> JsonValue:
    """Separate calls capture distinct observation instants; compare every other field."""
    if isinstance(value, dict):
        return {
            k: "<observation>" if k == "observed_at" else without_observation(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [without_observation(v) for v in value]
    return value


async def scope_continue(admission: dict[str, JsonValue], stage: str) -> dict[str, JsonValue]:
    """Discover from scope in a new process, then explicitly choose this fictional fixture."""
    consumer = ScopeConsumer(admission)
    scope = consumer.scope
    project = data(consumer.cli("project.get", {"project_id": scope["project_id"]}))
    association = data(consumer.cli("project.client.get", {"project_id": scope["project_id"]}))
    assert association["client_id"] == scope["client_id"]
    assert project["storyline_id"] == scope["storyline_id"]
    drafts = data(consumer.cli("recipe.draft.list", {}))["recipes"]
    brands = data(consumer.cli("brand.draft.list", {"client_id": scope["client_id"]}))["brands"]
    assert isinstance(drafts, list) and len(drafts) == 1
    assert isinstance(brands, list) and len(brands) == 1
    recipe_id = object_value(drafts[0])["recipe_id"]
    brand_id = object_value(brands[0])["brand_id"]
    recipes = consumer.pages("recipe.version.list", {"recipe_id": recipe_id})
    versions = consumer.pages(
        "brand.version.list", {"client_id": scope["client_id"], "brand_id": brand_id}
    )
    approvals: list[dict[str, JsonValue]] = []
    for action_scope in (
        {"kind": "recipe", "recipe_id": recipe_id},
        {"kind": "brand", "brand_id": brand_id, "client_id": scope["client_id"]},
        {"kind": "project", **scope},
    ):
        approvals.extend(consumer.pages("approval.list", {"scope": action_scope}))
    outcomes: list[dict[str, JsonValue]] = []
    for record in approvals:
        outcomes.append(
            data(
                consumer.cli(
                    "approval.outcome.get",
                    {key: record[key] for key in ("application_instance_id", "challenge_id")},
                )
            )
        )
    context = data(consumer.cli("project.recipe.context", scope))
    bindings = consumer.pages("project.recipe.context", scope)
    grants = consumer.pages("grant.list", scope)
    result: dict[str, JsonValue] = {
        "pending_challenge_ids": [
            row["challenge_id"] for row in outcomes if row["status"] == "pending"
        ],
        "completed_challenge_ids": [
            row["challenge_id"] for row in outcomes if row["status"] == "completed"
        ],
        "current_policy": object_value(context["scope"])["current_policy_revision"],
        "actor": admission["actor"],
    }
    consumer.log.record(
        "explicit-fixture-choice",
        recipe_id=recipe_id,
        brand_id=brand_id,
        rationale="Only fictional recipe and client brand; no implicit application choice",
    )
    async with consumer.connect("mcp-b") as sdk:
        capabilities = data(await consumer.mcp(sdk, "capabilities", {}))
        operations = capabilities["operations"]
        assert isinstance(operations, list) and len(operations) == 79
        assert without_observation(
            data(await consumer.mcp(sdk, "project.recipe.context", scope))
        ) == without_observation(context)
        if stage == "pending":
            pending_ids = result["pending_challenge_ids"]
            assert isinstance(pending_ids, list) and len(pending_ids) == 1
            assert not bindings and not grants and not recipes and not versions
        elif stage in {"render", "rebound"}:
            policy = result["current_policy"]
            eligible_bindings = [b for b in bindings if b["config_policy_revision"] == policy]
            assert len(eligible_bindings) == 1
            config = object_value(eligible_bindings[0]["config"])
            assert any(v["version_id"] == config["recipe_version_id"] for v in recipes)
            assert any(v["version_id"] == config["brand_version_id"] for v in versions)
            # The fixture has one current unrevoked grant; runtime still decides permission.
            choices = [
                g
                for g in grants
                if g["revoked_at"] is None
                and object_value(g["terms"])["config_policy_revision"] == policy
            ]
            assert len(choices) == 1
            grant = choices[0]
            for kind in ("recipe", "brand"):
                pin = object_value(object_value(grant["terms"])[kind])
                assert pin[kind + "_id"] == config[kind + "_id"]
                assert pin["version_id"] == config[kind + "_version_id"]
                assert pin["content_sha256"] == config[kind + "_sha256"]
            activity = data(
                consumer.cli("project.activity.get", {"project_id": scope["project_id"]})
            )
            opened_here = activity["current_cycle"] is None
            if opened_here:
                activity = data(
                    consumer.cli(
                        "project.cycle.open",
                        {
                            "project_id": scope["project_id"],
                            "expected_activity_revision": activity["revision"],
                        },
                    )
                )
            request = scope | {
                "config_id": config["config_id"],
                "config_revision": config["config_revision"],
                "config_sha256": config["config_sha256"],
                "grant_id": grant["grant_id"],
                "expected_project_revision": project["revision"],
                "idempotency_key": "scope-" + stage,
            }
            consumer.log.record(
                "explicit-render-choice",
                config=config,
                grant=grant,
                policy=policy,
                no_read_derived_permission=True,
            )
            job = data(await consumer.mcp(sdk, "graphics.render_lower_third", request))
            assert job["status"] == "succeeded"
            assert data(consumer.cli("graphics.render_lower_third", request)) == job
            artifact = data(
                consumer.cli(
                    "artifact.get",
                    scope | {"artifact_id": object_value(job["result_ref"])["artifact_id"]},
                )
            )
            assert (
                data(
                    await consumer.mcp(
                        sdk,
                        "artifact.get",
                        scope | {"artifact_id": object_value(job["result_ref"])["artifact_id"]},
                    )
                )
                == artifact
            )
            result.update(render_request=request, job=job, artifact=artifact)
            if opened_here:
                consumer.cli(
                    "project.cycle.close",
                    {
                        "project_id": scope["project_id"],
                        "cycle_id": object_value(activity["current_cycle"])["cycle_id"],
                        "expected_activity_revision": activity["revision"],
                        "outcome": "completed",
                    },
                )
        else:
            assert stage == "reviews"
            jobs = data(consumer.cli("render.job.list", scope))["jobs"]
            assert isinstance(jobs, list) and len(jobs) == 1
            artifact_id = object_value(object_value(jobs[0])["result_ref"])["artifact_id"]
            request = scope | {"artifact_id": artifact_id}
            reviews = consumer.pages("output.review.list", request)
            page = data(await consumer.mcp(sdk, "output.review.list", request))
            latest = object_value(page["latest_review"])
            assert latest["decision"] == "approved" and latest in reviews
            result["latest_review"] = latest
    consumer.log.record(
        "scope-only-complete",
        stage=stage,
        result=result,
        owner_channel_used=False,
        human_consent=False,
    )
    return result


def installed_identity(wheel: Path) -> dict[str, JsonValue]:
    """Compare every wheel package byte and installed RECORD digest outside the checkout."""
    package = distribution("video-genie")
    direct = object_value(json.loads(package.read_text("direct_url.json") or "{}"))
    assert not object_value(direct.get("dir_info", {})).get("editable", False)
    files: dict[str, JsonValue] = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith("videogenie/") and not name.endswith("/"):
                path = Path(str(package.locate_file(name))).resolve()
                assert path.is_relative_to(Path(sys.prefix).resolve())
                actual = path.read_bytes()
                assert actual == archive.read(name), name
                files[name] = sha(actual)
    checked = 0
    for name, digest, size in csv.reader(io.StringIO(package.read_text("RECORD") or "")):
        if not digest:
            continue
        algorithm, expected = digest.split("=", 1)
        assert algorithm == "sha256"
        actual = Path(str(package.locate_file(name))).read_bytes()
        assert encoded(hashlib.sha256(actual).digest()) == expected and len(actual) == int(size)
        checked += 1
    return {
        "wheel_sha256": sha(wheel.read_bytes()),
        "package_files": files,
        "record_entries_verified": checked,
        "noneditable": True,
        "python_isolated_mode": bool(sys.flags.isolated),
        "direct_url": direct,
    }


def run_consumer(
    console: OwnedConsole,
    log: Transcript,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 360,
) -> None:
    """Retain direct custody of the console when its separate consumer exits or times out."""
    try:
        console.start()
        log.command(argv + ["--owner-port", str(console.port)], cwd=cwd, env=env, timeout=timeout)
    except BaseException as failure:
        try:
            console.close()
        except BaseException as cleanup:
            failure.add_note(
                f"Owned console cleanup also failed: {type(cleanup).__name__}: {cleanup}"
            )
        raise
    else:
        console.close()


def bootstrap(arguments: argparse.Namespace) -> Path:
    """Install one exact wheel with frozen hashed runtime dependencies in an external directory."""
    repository = Path(__file__).resolve().parents[1]
    root = (
        Path(arguments.output_dir).resolve()
        if arguments.output_dir
        else Path(tempfile.mkdtemp(prefix="videogenie-recipe-proof-"))
    )
    assert not root.is_relative_to(repository)
    root.mkdir(parents=True, exist_ok=True)
    assert not list(root.iterdir()), "Use a fresh proof directory; retain previous failures"
    log = Transcript(root)
    log.path = root / "bootstrap.json"
    uv = shutil.which("uv")
    assert uv
    supplied = arguments.wheel or os.environ.get("VIDEOGENIE_TEST_WHEEL")
    if supplied:
        wheel = Path(supplied)
        assert wheel.is_absolute() and wheel.is_file() and wheel.suffix == ".whl"
    else:
        log.command(
            [uv, "build", "--wheel", "--out-dir", str(root / "dist")], cwd=repository, timeout=60
        )
        wheel = next((root / "dist").glob("*.whl"))
    revision = arguments.source_revision or os.environ.get("GITHUB_SHA") or "unverified"
    log.record(
        "artifact",
        wheel=str(wheel),
        sha256=sha(wheel.read_bytes()),
        source_revision=revision,
        supplied=bool(supplied),
    )
    runtime = root / "runtime"
    log.command([uv, "venv", "--python", sys.executable, str(runtime)], cwd=root)
    python = runtime / "bin" / "python"
    requirements = root / "runtime-requirements.txt"
    log.command(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ],
        cwd=repository,
    )
    log.command(
        [uv, "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)],
        cwd=root,
        timeout=90,
    )
    log.command(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        cwd=root,
        timeout=60,
    )
    for source in (Path(__file__), repository / "scripts" / "dogfood_external_agent.py"):
        shutil.copyfile(source, root / source.name)
    fonts = root / "fixture"
    fonts.mkdir()
    for name in ("Inter-Bold.ttf", "OFL.txt", "fonts.lock.json"):
        shutil.copyfile(repository / "tests" / "fixtures" / "fonts" / name, fonts / name)
    env = consumer_environment(root, runtime)
    launcher = (
        "import runpy,sys;root=sys.argv[1];sys.path.insert(0,root);"
        "sys.argv=[root+'/dogfood_recipe_render.py',*sys.argv[2:]];"
        "runpy.run_path(sys.argv[0],run_name='__main__')"
    )
    console = OwnedConsole(root, runtime, env)
    run_consumer(
        console,
        log,
        [
            str(python),
            "-I",
            "-c",
            launcher,
            str(root),
            "--consume",
            str(root),
            "--wheel",
            str(wheel),
        ],
        cwd=root,
        env=env,
        timeout=360,
    )
    return root


def consume(root: Path, wheel: Path, owner_port: int) -> None:
    """Run the repeatable public transport proof; native-host evidence is recorded separately."""
    log = Transcript(root)
    log.path = root / "consumer-transcript.json"
    log.record("installed-identity", **installed_identity(wheel))
    consumer = RecipeConsumer(root, log)
    owner = SyntheticOwner(consumer, owner_port)
    owner.start()
    asyncio.run(consumer.recipe_scenario(owner))


def interrupted(signum: int, frame: object) -> None:
    """Let owned-child finally blocks run on a supervisor's bounded termination."""
    raise KeyboardInterrupt(f"Proof supervisor signal {signum}")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir")
    parser.add_argument("--wheel")
    parser.add_argument("--source-revision")
    parser.add_argument("--consume")
    parser.add_argument("--owner-port", type=int)
    parser.add_argument("--scope-stage", choices=("pending", "render", "reviews", "rebound"))
    args = parser.parse_args()
    if args.scope_stage:
        admission = object_value(json.load(sys.stdin))
        print(json.dumps(asyncio.run(scope_continue(admission, args.scope_stage))))
    elif args.consume:
        consume(Path(args.consume), Path(args.wheel), args.owner_port)
    else:
        print(bootstrap(args))
