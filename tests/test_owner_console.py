"""Real loopback HTTP and synthetic signatures; no real browser/owner ceremony."""

import hashlib
import http.client
import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from test_job_storage import claim, seed, snapshot
from test_job_storage import registry as storage_registry
from test_owner_auth import Authenticator
from test_recipes import content

from videogenie.adapters import owner_console as module
from videogenie.adapters.owner_auth import EnrollmentTicket, OwnerAuth
from videogenie.adapters.owner_console import ConsoleLaunch, OwnerConsole
from videogenie.contracts.authority import (
    OwnerActionRequest,
    OwnerChallengeInput,
    PublishRecipeAction,
)
from videogenie.contracts.recipes import CreateRecipeDraftInput
from videogenie.contracts.rendering import RenderManifest
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.job_storage import JobStorage
from videogenie.core.recipes import RecipeService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

# Keep the imported real-storage fixture visible to pytest without invoking its internals.
assert storage_registry


@dataclass
class Browser:
    cookie: str
    csrf: str


def request(
    console: OwnerConsole,
    path: str,
    *,
    method: str = "POST",
    body: str = "{}",
    browser: Browser | None = None,
    extra: list[tuple[str, str]] | None = None,
    omit: tuple[str, ...] = (),
) -> tuple[int, dict[str, str], bytes]:
    headers = [("Host", console.host), ("Origin", console.origin)]
    if method == "POST":
        headers += [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body.encode()))),
        ]
    if browser:
        headers += [("Cookie", browser.cookie), ("X-CSRF-Token", browser.csrf)]
    headers = [(key, value) for key, value in headers if key not in omit] + (extra or [])
    connection = http.client.HTTPConnection("127.0.0.1", console.server.server_port, timeout=5)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for key, value in headers:
            connection.putheader(key, value)
        connection.endheaders(body.encode() if method == "POST" else None)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def login(console: OwnerConsole, launch: ConsoleLaunch | None = None) -> Browser:
    launch = launch or console.open_session()
    status, headers, raw = request(console, "/session", extra=[("X-Console-Launch", launch.token)])
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert headers["Cache-Control"] == "no-store"
    return Browser(headers["Set-Cookie"].split(";", 1)[0], json.loads(raw)["csrf"])


@pytest.fixture
def console(
    storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest],
) -> Iterator[OwnerConsole]:
    service, _, _ = storage_registry
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    authority = OwnerAuthorityService(
        RecipeService(StudioService(service)), OwnerAuth(service, origin=f"http://localhost:{port}")
    )
    server = OwnerConsole(authority)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.server.shutdown()
        thread.join(timeout=10)
        server.close()
        assert not thread.is_alive()


def enrollment(console: OwnerConsole) -> tuple[Browser, Authenticator, EnrollmentTicket]:
    ticket = console.authority.auth.begin_bootstrap()
    browser = login(console, console.open_session(enrollment=ticket))
    status, _, body = request(console, "/enrollment/options", browser=browser)
    assert status == 200
    data = json.loads(body)
    assert "bearer" not in data and data["next_recovery_secret"]
    signer = Authenticator()
    status, _, body = request(
        console,
        "/enrollment/complete",
        browser=browser,
        body=signer.register(ticket, origin=console.origin).model_dump_json(),
    )
    assert status == 200 and json.loads(body)["data"]["user_verified"] is True
    return browser, signer, ticket


def action(console: OwnerConsole) -> OwnerActionRequest:
    draft = console.authority.recipes.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key=str(uuid4()))
    )
    return OwnerActionRequest(
        action=PublishRecipeAction(
            recipe_id=draft.recipe_id,
            expected_draft_revision=1,
            content_sha256=draft.content_sha256,
        ),
        idempotency_key=str(uuid4()),
    )


def counts(console: OwnerConsole) -> tuple[int, int]:
    with console.authority.service.connection() as connection:
        return (
            connection.execute("SELECT COUNT(*) FROM owner_receipts").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0],
        )


def test_loopback_launch_single_use_and_no_http_bootstrap(
    console: OwnerConsole, capsys: pytest.CaptureFixture[str]
) -> None:
    assert console.server.server_address[0] == "127.0.0.1"
    launch = console.open_session()
    assert launch.token not in repr(launch)
    assert "#launch=" in launch.url and "?" not in launch.url
    browser = login(console, launch)
    assert request(console, "/session", extra=[("X-Console-Launch", launch.token)])[0] == 403
    assert request(console, "/session")[0] == 403
    assert request(console, "/bootstrap", browser=browser)[0] == 404
    assert request(console, "/enrollment/options", browser=browser)[0] == 403
    status, headers, body = request(console, "/", method="GET")
    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "<title>VideoGenie · Owner review</title>" in body.decode("utf-8")
    assert b"<h1>Owner review</h1>" in body
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    csp = headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert not capsys.readouterr().err


@pytest.mark.parametrize(
    "header,value",
    [
        ("Host", "evil.example"),
        ("Origin", "http://evil.example"),
        ("Origin", "null"),
        ("X-CSRF-Token", "incorrect"),
        ("X-CSRF-Token", "é"),
        ("Sec-Fetch-Site", "cross-site"),
    ],
)
def test_reject_wrong_context_without_mutation(
    console: OwnerConsole, header: str, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = login(console)
    before = counts(console)
    status, _, body = request(
        console,
        "/action/request",
        browser=browser,
        body=action(console).model_dump_json(),
        omit=(header,),
        extra=[(header, value)],
    )
    assert status == 403 and json.loads(body)["ok"] is False
    assert counts(console) == before
    assert not capsys.readouterr().err


@pytest.mark.parametrize(
    "header", ["Host", "Origin", "X-CSRF-Token", "Cookie", "Content-Length", "Content-Type"]
)
def test_duplicate_headers_reject(console: OwnerConsole, header: str) -> None:
    browser = login(console)
    value = {
        "Host": console.host,
        "Origin": console.origin,
        "X-CSRF-Token": browser.csrf,
        "Cookie": browser.cookie,
        "Content-Length": "2",
        "Content-Type": "application/json",
    }[header]
    assert request(console, "/logout", browser=browser, extra=[(header, value)])[0] in {400, 403}
    assert request(console, "/logout", browser=browser)[0] == 200


def test_duplicate_cookie_json_and_transfer_rejected(console: OwnerConsole) -> None:
    browser = login(console)
    assert (
        request(
            console,
            "/logout",
            browser=browser,
            omit=("Cookie",),
            extra=[("Cookie", browser.cookie + "; " + browser.cookie)],
        )[0]
        == 403
    )
    assert request(console, "/logout", browser=browser, body='{"x":1,"x":2}')[0] == 400
    assert (
        request(console, "/logout", browser=browser, extra=[("Transfer-Encoding", "chunked")])[0]
        == 400
    )
    assert request(console, "/logout?token=bad", browser=browser)[0] == 400
    assert (
        request(
            console,
            "/logout",
            browser=browser,
            omit=("Content-Length",),
            extra=[("Content-Length", "1048577")],
        )[0]
        == 413
    )


def test_launch_and_session_expiry_no_renewal(
    console: OwnerConsole, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = module._clock()
    monkeypatch.setattr(module, "_clock", lambda: now)
    launch = console.open_session()
    now += module._LAUNCH_SECONDS
    assert request(console, "/session", extra=[("X-Console-Launch", launch.token)])[0] == 403
    browser = login(console)
    start = now
    now += 100
    assert request(console, "/enrollment/options", browser=browser)[0] == 403
    now = start + module._SESSION_SECONDS
    status, _, body = request(console, "/logout", browser=browser)
    assert status == 403 and json.loads(body)["error"]["reason"] == "session_expired"


def test_enrollment_scoped_options_secret_and_recovery(console: OwnerConsole) -> None:
    other = login(console)
    browser, signer, ticket = enrollment(console)
    assert request(console, "/enrollment/options", browser=browser)[0] == 403
    assert request(console, "/logout", browser=other)[0] == 403
    assert (
        request(
            console,
            "/enrollment/complete",
            browser=browser,
            body=signer.register(ticket, origin=console.origin).model_dump_json(),
        )[0]
        == 200
    )
    replacement = console.authority.auth.begin_recovery(ticket.next_recovery_secret)
    next_browser = login(console, console.open_session(enrollment=replacement))
    next_signer = Authenticator()
    status, _, _ = request(
        console,
        "/enrollment/complete",
        browser=next_browser,
        body=next_signer.register(replacement, origin=console.origin).model_dump_json(),
    )
    assert status == 200
    assert request(console, "/logout", browser=browser)[0] == 403


def test_actual_signed_selected_action_matches_core_and_replay(console: OwnerConsole) -> None:
    browser, signer, _ = enrollment(console)
    target = console.authority.request_action(action(console))
    proof = signer.assert_action(target, count=0, change={"origin": console.origin})
    assert (
        request(console, "/action/apply", browser=browser, body=proof.model_dump_json())[0] == 403
    )
    lookup = OwnerChallengeInput(
        application_instance_id=target.application_instance_id, challenge_id=target.challenge_id
    )
    status, _, body = request(
        console, "/action/select", browser=browser, body=lookup.model_dump_json()
    )
    assert status == 200 and json.loads(body)["data"]["action_sha256"] == target.action_sha256
    status, _, raw = request(
        console, "/action/apply", browser=browser, body=proof.model_dump_json()
    )
    assert status == 200
    assert json.loads(raw)["data"] == console.authority.apply_action(proof).model_dump(mode="json")
    before = counts(console)
    assert (
        request(console, "/action/apply", browser=browser, body=proof.model_dump_json())[2] == raw
    )
    assert counts(console) == before
    other = console.authority.request_action(action(console))
    other_proof = signer.assert_action(other, count=0, change={"origin": console.origin})
    assert (
        request(console, "/action/apply", browser=browser, body=other_proof.model_dump_json())[0]
        == 403
    )
    assert counts(console) == before


def test_invalid_uv_and_stale_revision_stay_unconsumed(console: OwnerConsole) -> None:
    from videogenie.contracts.recipes import UpdateRecipeDraftInput

    browser, signer, _ = enrollment(console)
    target = console.authority.request_action(action(console))
    request(
        console,
        "/action/select",
        browser=browser,
        body=OwnerChallengeInput(
            application_instance_id=target.application_instance_id, challenge_id=target.challenge_id
        ).model_dump_json(),
    )
    bad = signer.assert_action(target, flags=1, count=0, change={"origin": console.origin})
    before = counts(console)
    assert request(console, "/action/apply", browser=browser, body=bad.model_dump_json())[0] == 403
    assert counts(console) == before
    selected = target.action
    assert isinstance(selected, PublishRecipeAction)
    console.authority.recipes.update_recipe_draft(
        UpdateRecipeDraftInput(
            recipe_id=selected.recipe_id,
            expected_draft_revision=1,
            content=content(60),
            idempotency_key="edit",
        )
    )
    proof = signer.assert_action(target, count=0, change={"origin": console.origin})
    assert (
        request(console, "/action/apply", browser=browser, body=proof.model_dump_json())[0] == 409
    )
    assert counts(console) == before


def artifact(
    console: OwnerConsole, registry: tuple[VideoGenieService, JobStorage, RenderManifest]
) -> tuple[str, Path, bytes]:
    service, storage, template = registry
    payload = b"synthetic-not-a-rendered-video"
    template = RenderManifest.model_validate_json(
        json.dumps(
            template.model_dump(mode="json")
            | {"artifact_sha256": hashlib.sha256(payload).hexdigest(), "byte_size": len(payload)}
        )
    )
    seed(service, template, "graphics.render_lower_third")
    staged = snapshot(template, claim(service, storage, template, "graphics.render_lower_third"))
    with service.connection(write=True) as connection:
        for state in ("staged", "verified", "published"):
            storage.record_manifest(
                connection,
                RenderManifest.model_validate_json(
                    json.dumps(staged.model_dump(mode="json") | {"state": state})
                ),
            )
    path = service.data_dir / template.locator
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    url = (
        f"/artifacts/{template.request.project_id}/{template.request.storyline_id}"
        f"/{template.job_id}/{template.artifact_id}"
    )
    return url, path, payload


def test_id_scoped_published_artifact_exact_bytes(
    console: OwnerConsole, storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest]
) -> None:
    url, path, payload = artifact(console, storage_registry)
    browser = login(console)
    assert request(console, url, method="GET")[0] == 403
    status, headers, body = request(console, url, method="GET", browser=browser)
    assert status == 200 and body == payload and headers["Content-Type"] == "video/quicktime"
    for index in (2, 3, 4, 5):
        parts = url.split("/")
        parts[index] = str(uuid4())
        assert request(console, "/".join(parts), method="GET", browser=browser)[0] == 404
    path.write_bytes(b"x" * len(payload))
    assert request(console, url, method="GET", browser=browser)[0] == 409


@pytest.mark.parametrize("mode", ["file_symlink", "dir_symlink", "traversal"])
def test_artifact_escape_rejects(
    console: OwnerConsole,
    storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    tmp_path: Path,
    mode: str,
) -> None:
    url, path, payload = artifact(console, storage_registry)
    browser = login(console)
    outside = tmp_path / "private.bin"
    outside.write_bytes(payload)
    if mode == "file_symlink":
        path.unlink()
        path.symlink_to(outside)
    elif mode == "dir_symlink":
        path.unlink()
        path.parent.rmdir()
        path.parent.symlink_to(tmp_path, target_is_directory=True)
        (tmp_path / path.name).write_bytes(payload)
    else:
        url = "/artifacts/../../private.bin"
    assert request(console, url, method="GET", browser=browser)[0] in {400, 403, 404}


def test_artifact_response_uses_verified_open_snapshot(
    console: OwnerConsole,
    storage_registry: tuple[VideoGenieService, JobStorage, RenderManifest],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    url, path, payload = artifact(console, storage_registry)
    browser = login(console)
    original = os.fdopen

    def replace_path(descriptor: int, mode: str) -> io.BufferedReader:
        stream = original(descriptor, mode)
        assert isinstance(stream, io.BufferedReader)
        path.unlink()
        path.write_bytes(b"replacement bytes")
        return stream

    monkeypatch.setattr(os, "fdopen", replace_path)
    status, _, body = request(console, url, method="GET", browser=browser)
    assert status == 200 and body == payload
    assert path.read_bytes() == b"replacement bytes"


@pytest.mark.parametrize("header", ["Host", "Origin", "X-CSRF-Token"])
def test_missing_security_header_rejects(console: OwnerConsole, header: str) -> None:
    browser = login(console)
    assert request(console, "/logout", browser=browser, omit=(header,))[0] == 403
    assert request(console, "/logout", browser=browser)[0] == 200


def test_session_capacity_is_bounded(console: OwnerConsole) -> None:
    from videogenie.contracts.models import VideoGenieError

    for _ in range(module._MAX_SESSIONS):
        console.open_session()
    with pytest.raises(VideoGenieError):
        console.open_session()
