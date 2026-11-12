"""Installed foreground owner launch with real HTTP and isolated synthetic credentials."""

import fcntl
import hashlib
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
import termios
import time
from collections.abc import Mapping
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import JsonValue, TypeAdapter
from test_owner_auth import Authenticator
from test_recipes import content

from videogenie.adapters.owner_auth import EnrollmentTicket, OwnerAuth
from videogenie.contracts.authority import (
    OwnerActionRequest,
    OwnerEnrollmentChallenge,
    PublishRecipeAction,
)
from videogenie.contracts.recipes import CreateRecipeDraftInput
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.recipes import RecipeService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("owner-installed")
    repo = Path(__file__).resolve().parents[1]
    uv = shutil.which("uv")
    assert uv
    wheel = (
        Path(os.environ["VIDEOGENIE_TEST_WHEEL"]) if "VIDEOGENIE_TEST_WHEEL" in os.environ else None
    )
    if wheel is None:
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(root / "dist")],
            cwd=repo,
            check=True,
            capture_output=True,
            timeout=60,
        )
        wheel = next((root / "dist").glob("*.whl"))
    assert wheel.is_absolute() and wheel.is_file()
    runtime = root / "runtime"
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(runtime)],
        check=True,
        capture_output=True,
        timeout=30,
    )
    python = runtime / "bin" / "python"
    requirements = root / "requirements.txt"
    subprocess.run(
        [
            uv,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [uv, "pip", "sync", "--python", str(python), "--require-hashes", str(requirements)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        timeout=60,
    )
    proof = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import pathlib,sys,videogenie;"
            "from importlib.resources import files;"
            "from importlib.metadata import distribution;"
            "import json;"
            "module=pathlib.Path(videogenie.__file__).resolve();"
            "assert module.is_relative_to(pathlib.Path(sys.prefix));"
            "direct=distribution('video-genie').read_text('direct_url.json');"
            "assert not json.loads(direct).get('dir_info',{}).get('editable',False);"
            "assert files('videogenie.adapters').joinpath('owner_console.html').is_file();"
            "print('installed-resources-ok')",
        ],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    (root / "receipt.json").write_text(
        json.dumps(
            {
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "lock_sha256": hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest(),
                "proof": proof.stdout,
            }
        )
    )
    return runtime / "bin" / "videogenie"


def env() -> dict[str, str]:
    result = os.environ.copy()
    result.pop("PYTHONPATH", None)
    result.pop("VIDEOGENIE_DATA_DIR", None)
    return result


def port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch(
    executable: Path,
    root: Path,
    mode: str,
    number: int,
    *,
    challenge: str | None = None,
    terminal: bool = False,
    inject: str | None = None,
) -> tuple[subprocess.Popen[str], Path, int | None]:
    private = root / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    handoff = private / (str(uuid4()) + ".txt")
    args = [
        str(executable),
        "--data-dir",
        str(root / "state"),
        "owner",
        mode,
        "--port",
        str(number),
        "--handoff-file",
        str(handoff),
    ]
    if challenge:
        args += ["--challenge-id", challenge]
    if inject is not None:
        args = [str(executable.with_name("python")), "-I", "-c", inject, *args[1:]]
    master: int | None = None
    if terminal:
        master, slave = pty.openpty()

        def control() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        child = subprocess.Popen(
            args,
            cwd=root,
            env=env(),
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=control,
        )
        os.close(slave)
    else:
        child = subprocess.Popen(
            args,
            cwd=root,
            env=env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    return child, handoff, master


def ready(child: subprocess.Popen[str]) -> dict[str, JsonValue]:
    assert child.stdout is not None
    assert select.select([child.stdout], [], [], 10)[0], "CLI did not emit a bounded ready result"
    payload = json.loads(child.stdout.readline())
    assert payload["success"], payload
    return TypeAdapter(dict[str, JsonValue]).validate_python(payload["data"])


def supervise(master: int, answer: bytes) -> bytes:
    assert select.select([master], [], [], 10)[0]
    prompt = os.read(master, 4096)
    os.write(master, answer + b"\n")
    return prompt


def stop(
    child: subprocess.Popen[str], handoff: Path, master: int | None = None
) -> tuple[str, str, bytes]:
    child.send_signal(signal.SIGTERM)
    terminal_tail = bytearray()
    # Darwin PTY session exit can wait for the terminal emulator to drain output.
    # A real terminal does this continuously; the test harness must too.
    if master is not None:
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    block = os.read(master, 4096)
                except OSError:
                    break
                if not block:
                    break
                terminal_tail.extend(block)
    out, err = child.communicate(timeout=10)
    if master is not None:
        os.close(master)
    assert child.returncode == 0 and out == "" and err == "", (child.returncode, out, err)
    assert not handoff.exists()
    return out, err, bytes(terminal_tail)


def http(
    number: int,
    route: str,
    *,
    body: Mapping[str, object] | None = None,
    cookie: str | None = None,
    csrf: str | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    connection = HTTPConnection("127.0.0.1", number, timeout=5)
    headers = {"Host": f"localhost:{number}", "Origin": f"http://localhost:{number}"}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-CSRF-Token"] = csrf
    if token:
        headers["X-Console-Launch"] = token
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(
            "POST" if body is not None else "GET",
            route,
            body=json.dumps(body) if body is not None else None,
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def session(number: int, handoff: Path) -> tuple[str, str, str]:
    assert stat.S_IMODE(handoff.stat().st_mode) == 0o600
    url = handoff.read_text().splitlines()[1]
    token = parse_qs(urlsplit(url).fragment)["launch"][0]
    code, headers, raw = http(number, "/session", body={}, token=token)
    assert code == 200
    data = json.loads(raw)
    assert http(number, "/session", body={}, token=token)[0] == 403
    return headers["Set-Cookie"].split(";", 1)[0], data["csrf"], url


def test_installed_serve_outside_checkout_restart_and_exclusion(
    installed: Path, tmp_path: Path
) -> None:
    number = port()
    child, handoff, _ = launch(installed, tmp_path, "serve", number)
    try:
        data = ready(child)
        cookie, csrf, url = session(number, handoff)
        assert url not in json.dumps(data) and "launch=" not in json.dumps(data)
        code, headers, page = http(number, "/")
        assert code == 200 and b"VideoGenie" in page
        assert "unsafe-inline" not in headers["Content-Security-Policy"]
        assert http(number, "/enrollment/options", body={}, cookie=cookie, csrf=csrf)[0] == 403
        other, otherfile, _ = launch(installed, tmp_path, "serve", number)
        out, err = other.communicate(timeout=10)
        assert other.returncode == 4 and not otherfile.exists()
        assert json.loads(out)["success"] is False and url not in out + err
    finally:
        stop(child, handoff)
    again, newfile, _ = launch(installed, tmp_path, "serve", number)
    try:
        assert ready(again)["origin"] == f"http://localhost:{number}"
    finally:
        stop(again, newfile)
    assert not (tmp_path / ".cleo").exists()


@pytest.mark.parametrize("mode", ["enroll", "recover"])
def test_no_terminal_cannot_admit_enrollment(installed: Path, tmp_path: Path, mode: str) -> None:
    child, handoff, _ = launch(installed, tmp_path, mode, port())
    out, err = child.communicate(timeout=10)
    assert child.returncode == 4 and json.loads(out)["success"] is False
    assert not handoff.exists() and not (tmp_path / "state").exists()
    assert "Offline" not in out + err


def test_help_flags_and_handoff_failures_are_structured(installed: Path, tmp_path: Path) -> None:
    for args in (
        ["owner", "--help", "--json"],
        ["owner", "serve", "--actor", "owner"],
        ["owner", "recover", "--recovery-secret", "DO-NOT-ECHO-THIS"],
        ["project", "list", "--port", "8456"],
        ["owner", "serve", "--input-json", "-"],
    ):
        result = subprocess.run(
            [str(installed), "--data-dir", str(tmp_path / "state"), *args],
            cwd=tmp_path,
            env=env(),
            input="{}",
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert "DO-NOT-ECHO-THIS" not in result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["success"] == ("--help" in args)
    assert not (tmp_path / "state").exists()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    handoff = private / "retained"
    handoff.write_text("keep")
    for path in (handoff, tmp_path / "public.txt"):
        if path.name == "public.txt":
            tmp_path.chmod(0o755)
        result = subprocess.run(
            [
                str(installed),
                "--data-dir",
                str(tmp_path / "state"),
                "owner",
                "serve",
                "--handoff-file",
                str(path),
            ],
            cwd=tmp_path,
            env=env(),
            text=True,
            capture_output=True,
            timeout=10,
        )
        assert result.returncode == 4 and json.loads(result.stdout)["success"] is False
    assert handoff.read_text() == "keep" and not (tmp_path / "state").exists()


def test_synthetic_terminal_enrollment_review_and_recovery(installed: Path, tmp_path: Path) -> None:
    number = port()
    origin = f"http://localhost:{number}"
    signer = Authenticator()
    child, handoff, master = launch(installed, tmp_path, "enroll", number, terminal=True)
    assert master is not None
    try:
        assert b"ENROLL" in supervise(master, b"ENROLL")
        ready(child)
        cookie, csrf, url = session(number, handoff)
        code, _, raw = http(number, "/enrollment/options", body={}, cookie=cookie, csrf=csrf)
        assert code == 200
        options = json.loads(raw)
        recovery = options["next_recovery_secret"]
        ticket = EnrollmentTicket(
            OwnerEnrollmentChallenge.model_validate(options["challenge"]),
            b"test-not-authority",
            b"test-not-authority",
        )
        registration = signer.register(ticket, origin=origin)
        code, _, raw = http(
            number,
            "/enrollment/complete",
            body=registration.model_dump(mode="json"),
            cookie=cookie,
            csrf=csrf,
        )
        assert code == 200
        assert recovery not in raw.decode() and url not in raw.decode()
    finally:
        stop(child, handoff, master)
    service = VideoGenieService(tmp_path / "state")
    recipes = RecipeService(StudioService(service))
    auth = OwnerAuth(service, origin=origin)
    authority = OwnerAuthorityService(recipes, auth)
    draft = recipes.create_recipe_draft(
        CreateRecipeDraftInput(content=content(), idempotency_key="review-draft")
    )
    challenge = authority.request_action(
        OwnerActionRequest(
            action=PublishRecipeAction(
                recipe_id=draft.recipe_id,
                expected_draft_revision=draft.draft_revision,
                content_sha256=draft.content_sha256,
            ),
            idempotency_key="challenge",
        )
    )
    review, handoff, _ = launch(
        installed, tmp_path, "review", number, challenge=challenge.challenge_id
    )
    try:
        data = ready(review)
        assert data["action_sha256"] == challenge.action_sha256
        cookie, csrf, _ = session(number, handoff)
        identity = {
            "application_instance_id": challenge.application_instance_id,
            "challenge_id": challenge.challenge_id,
        }
        assert http(number, "/action/select", body=identity, cookie=cookie, csrf=csrf)[0] == 200
        # Real signed synthetic proof; an unverified/malformed object cannot self-approve.
        assert http(number, "/action/apply", body=identity, cookie=cookie, csrf=csrf)[0] == 400
        proof = signer.assert_action(challenge, change={"origin": origin}).model_dump(mode="json")
        first = http(number, "/action/apply", body=proof, cookie=cookie, csrf=csrf)
        assert (
            first[0] == 200
            and http(number, "/action/apply", body=proof, cookie=cookie, csrf=csrf)[2] == first[2]
        )
    finally:
        stop(review, handoff)
    recovered, handoff, master = launch(installed, tmp_path, "recover", number, terminal=True)
    assert master is not None
    try:
        assert b"recovery" in supervise(master, recovery.encode())
        data = ready(recovered)
        assert recovery not in json.dumps(data)
        cookie, csrf, _ = session(number, handoff)
        code, _, raw = http(number, "/enrollment/options", body={}, cookie=cookie, csrf=csrf)
        assert code == 200
        new_options = json.loads(raw)
        new_ticket = EnrollmentTicket(
            OwnerEnrollmentChallenge.model_validate(new_options["challenge"]),
            b"test-not-authority",
            b"test-not-authority",
        )
        new_signer = Authenticator()
        assert (
            http(
                number,
                "/enrollment/complete",
                body=new_signer.register(new_ticket, origin=origin).model_dump(mode="json"),
                cookie=cookie,
                csrf=csrf,
            )[0]
            == 200
        )
    finally:
        _, _, terminal_tail = stop(recovered, handoff, master)
        assert recovery.encode() not in terminal_tail, "Recovery input must not be echoed"
    with service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM content_versions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM authority_grants").fetchone()[0] == 0


def test_recovery_no_echo_failure_never_reads_stdin_or_opens_state(
    installed: Path, tmp_path: Path
) -> None:
    injected = """import termios,sys
class NoInput:
    def readline(self,*args): raise AssertionError('ordinary stdin read')
    def read(self,*args): raise AssertionError('ordinary stdin read')
def fail(*args): raise termios.error('injected no-echo failure')
termios.tcgetattr=fail
sys.stdin=NoInput()
from videogenie.cli import main
sys.exit(main())
"""
    child, handoff, master = launch(
        installed, tmp_path, "recover", port(), terminal=True, inject=injected
    )
    assert master is not None
    # No answer is sent: failure must precede any fallback input request.
    out, err = child.communicate(timeout=10)
    os.close(master)
    payload = json.loads(out)
    assert (
        child.returncode == 4
        and payload["error"]["details"]["reason"] == "supervised_terminal_required"
    )
    assert "ordinary stdin" not in out + err and "Warning" not in out + err
    assert not handoff.exists() and not (tmp_path / "state").exists()


def test_failed_serving_thread_does_not_hang_or_leave_admission(
    installed: Path, tmp_path: Path
) -> None:
    injected = """import threading,sys
def fail(self): raise RuntimeError('injected start failure')
threading.Thread.start=fail
from videogenie.cli import main
sys.exit(main())
"""
    number = port()
    child, handoff, _ = launch(installed, tmp_path, "serve", number, inject=injected)
    out, err = child.communicate(timeout=10)
    assert child.returncode == 1 and json.loads(out)["success"] is False
    assert "launch=" not in out + err and not handoff.exists()
    with socket.socket() as available:
        available.bind(("127.0.0.1", number))


def test_cleanup_failure_is_visible_without_second_json_or_secret(
    installed: Path, tmp_path: Path
) -> None:
    injected = """import os,sys
def fail(*args,**kwargs): raise PermissionError('injected handoff cleanup failure')
os.unlink=fail
from videogenie.cli import main
sys.exit(main())
"""
    child, handoff, _ = launch(installed, tmp_path, "serve", port(), inject=injected)
    ready(child)
    private = handoff.read_text()
    child.send_signal(signal.SIGTERM)
    out, err = child.communicate(timeout=10)
    assert child.returncode == 1 and out == ""
    assert err == "ACCESS_DENIED: Private owner handoff cleanup unconfirmed\n"
    assert private not in err and "launch=" not in err and str(handoff) not in err
    assert handoff.exists()
