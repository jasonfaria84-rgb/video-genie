"""Explicit local owner launch; private admission never enters ordinary JSON output.

This is a cooperative same-OS-account boundary, not proof of human identity.
Only the browser's enrolled passkey can authorize a domain action.
"""

import getpass
import os
import re
import signal
import stat
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from types import FrameType

from webauthn.helpers import base64url_to_bytes

from videogenie.adapters.owner_auth import OwnerAuth
from videogenie.adapters.owner_console import OwnerConsole
from videogenie.contracts.authority import OwnerChallengeInput
from videogenie.contracts.models import ResultEnvelope, VideoGenieError
from videogenie.core.authority import OwnerAuthorityService
from videogenie.core.recipes import RecipeService
from videogenie.core.service import VideoGenieService
from videogenie.core.studio import StudioService

MODES = ("serve", "review", "enroll", "recover")
HELP = """usage: videogenie [--data-dir ABSOLUTE] owner MODE --handoff-file ABSOLUTE [--port PORT]

Modes: serve (open review console), review (--challenge-id prepared UUID),
       enroll (first supervised owner registration), recover (offline secret + fresh UV).
The port defaults to 8456 and is immutable for an existing registry; reuse its original port.
The new handoff file must be inside a private owner-only directory (0700); it is created 0600.
It contains a one-use launch URL. Open it privately, paste the URL into the local browser,
and never send it to an agent. Admission expires after 120 seconds; sessions after 10 minutes.
The process remains in the foreground until SIGINT/SIGTERM and removes its handoff file.
Stdout emits one JSON ready envelope without the launch token or recovery secret.
Serve/review do not enroll or approve. Enrollment requires explicit terminal supervision;
recovery reads only from the controlling terminal without echo. Every domain action still
requires fresh browser user verification. Same-account arbitrary code is outside this boundary.
No actual final-output approval endpoint is supplied yet.
"""


def _denied(reason: str) -> VideoGenieError:
    return VideoGenieError(
        "ACCESS_DENIED", "Owner console launch denied", details={"reason": reason}
    )


def _supervision(mode: str) -> bytes | None:
    if mode not in {"enroll", "recover"}:
        return None
    try:
        with open("/dev/tty", "w", encoding="utf-8") as terminal:
            if not terminal.isatty():
                raise _denied("supervised_terminal_required")
            if mode == "enroll":
                terminal.write("First owner registration. Type ENROLL to open supervised setup: ")
                terminal.flush()
                with open("/dev/tty", encoding="utf-8") as reader:
                    if reader.readline(64).strip() != "ENROLL":
                        raise _denied("enrollment_cancelled")
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                secret = getpass.getpass("Offline owner recovery key: ", stream=terminal)
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", secret) is None:
                raise _denied("recovery_secret")
            decoded = base64url_to_bytes(secret)
            if len(decoded) != 32:
                raise _denied("recovery_secret")
            return decoded
    except (OSError, EOFError, getpass.GetPassWarning) as exc:
        raise _denied("supervised_terminal_required") from exc


def run_owner(
    mode: str,
    *,
    data_dir: str | None,
    port: int,
    handoff_file: str,
    challenge_id: str | None,
) -> int:
    """Serve an explicitly launched console; emit one nonsecret ready envelope.

    No daemon, automatic browser, credential import or domain approval bypass.
    The handoff descriptor is exclusively created and only its own inode is removed.
    """
    if mode not in MODES or not 1 <= port <= 65535:
        raise VideoGenieError("INVALID_ARGUMENT", "Use a supported owner mode and fixed port")
    if (mode == "review") != (challenge_id is not None):
        raise VideoGenieError("INVALID_ARGUMENT", "Only owner review requires --challenge-id")
    path = Path(handoff_file)
    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise VideoGenieError("INVALID_ARGUMENT", "Use an absolute new private handoff file")
    # Resolve directory aliases (for example macOS /tmp), then retain the owned handle.
    parent = path.parent.resolve(strict=True)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptor: int | None = None
    identity: tuple[int, int] | None = None
    console: OwnerConsole | None = None
    thread: threading.Thread | None = None
    published = False
    serving = False
    stopped = threading.Event()
    previous: dict[int, Callable[[int, FrameType | None], object] | int | None] = {}
    try:
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise _denied("private_handoff_directory_required")
        recovery = _supervision(mode)
        descriptor = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        created = os.fstat(descriptor)
        identity = (created.st_dev, created.st_ino)
        service = VideoGenieService(data_dir)
        auth = OwnerAuth(service, origin=f"http://localhost:{port}")
        authority = OwnerAuthorityService(RecipeService(StudioService(service)), auth)
        console = OwnerConsole(authority)
        challenge = None
        if challenge_id is not None:
            challenge = authority.get_challenge(
                OwnerChallengeInput(
                    application_instance_id=auth.application_instance_id, challenge_id=challenge_id
                )
            )
        ticket = (
            auth.begin_bootstrap()
            if mode == "enroll"
            else auth.begin_recovery(recovery)
            if recovery is not None
            else None
        )
        recovery = None
        launch = console.open_session(enrollment=ticket)
        private = (
            "VideoGenie private owner launch (one use; expires in 120 seconds)\n"
            + launch.url
            + "\n"
        )
        if challenge is not None:
            private += (
                "Prepared challenge: "
                + challenge.challenge_id
                + "\nAction SHA256: "
                + challenge.action_sha256
                + "\n"
            )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = None
            output.write(private)
            output.flush()
            os.fsync(output.fileno())
        private = ""
        os.fsync(directory)

        def stop(signum: int, frame: FrameType | None) -> None:
            stopped.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
        thread = threading.Thread(target=console.serve_forever, daemon=True)
        thread.start()
        serving = True
        print(
            ResultEnvelope[dict[str, str | None]](
                success=True,
                data={
                    "mode": mode,
                    "origin": console.origin,
                    "handoff_file": str(parent / path.name),
                    "challenge_id": challenge.challenge_id if challenge else None,
                    "action_sha256": challenge.action_sha256 if challenge else None,
                    "state": "serving",
                    "approval": "none",
                },
            ).model_dump_json(),
            flush=True,
        )
        published = True
        stopped.wait()
        return 0
    finally:
        if serving and thread is not None and console is not None:
            console.server.shutdown()
            thread.join(timeout=5)
        if console is not None:
            console.close()
        for previous_signal, handler in previous.items():
            signal.signal(previous_signal, handler)
        if descriptor is not None:
            os.close(descriptor)
        if identity is not None:
            try:
                current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == identity:
                    os.unlink(path.name, dir_fd=directory)
                    os.fsync(directory)
            except FileNotFoundError:
                pass
            except OSError:
                # Once ready was emitted, never print a second JSON result or secret path.
                if not published:
                    raise
        os.close(directory)
