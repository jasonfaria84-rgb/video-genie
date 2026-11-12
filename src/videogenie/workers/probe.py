"""Bounded, read-only ffprobe execution and validated media metadata."""

import errno
import hashlib
import json
import math
import os
import select
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import closing
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from videogenie.contracts.models import ContractModel, VideoGenieError

MAX_OUTPUT_BYTES = 1024 * 1024


class Fingerprint(ContractModel):
    """Stable content identity captured without holding the database write lock."""

    sha256: str
    size: int
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


class ProbeStream(ContractModel):
    """Selected stream facts with exact rational timing, or null when unavailable."""

    index: int
    codec_type: str | None
    codec_name: str | None
    duration: str | None
    frame_rate: str | None
    sample_rate: str | None
    time_base: str | None
    width: int | None
    height: int | None
    channels: int | None
    nb_frames: int | None


class ProbeResult(ContractModel):
    """Validated metadata artifact tied to the source digest and actual ffprobe version."""

    input_path: str
    input_sha256: str
    input_size: int
    ffprobe_version: str
    format_name: str
    duration: str | None
    streams: tuple[ProbeStream, ...]


class _RawStream(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    index: int
    codec_type: str | None = None
    codec_name: str | None = None
    duration: str | None = None
    avg_frame_rate: str | None = None
    sample_rate: str | None = None
    time_base: str | None = None
    width: int | None = None
    height: int | None = None
    channels: int | None = None
    nb_frames: str | None = None


class _RawFormat(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    format_name: str
    duration: str | None = None


class _RawProbe(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    format: _RawFormat
    streams: list[_RawStream] = Field(min_length=1, max_length=1024)


def absolute_media_path(path: str) -> Path:
    """Accept only an explicit absolute local path, without shell expansion."""
    if not isinstance(path, str) or not path or any(ord(char) < 32 for char in path):
        raise VideoGenieError("INVALID_ARGUMENT", "Media path must be an absolute local file")
    try:
        path.encode("utf-8")
        result = Path(path)
        if not result.is_absolute():
            raise ValueError("relative path")
        return result.resolve()
    except (ValueError, OSError, UnicodeError) as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "Media path must be an absolute local file"
        ) from exc


def fingerprint(path: Path, timeout_seconds: float = 30.0) -> Fingerprint:
    """Hash a regular file while detecting replacement or mutation during the read."""
    deadline = time.monotonic() + timeout_seconds
    try:
        before = path.stat()
        if not stat.S_ISREG(before.st_mode):
            raise VideoGenieError("MEDIA_UNAVAILABLE", "Media input must be a regular file")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise VideoGenieError("INPUT_CHANGED", "Media changed while opening it")
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                if time.monotonic() > deadline:
                    raise VideoGenieError(
                        "MEDIA_UNAVAILABLE", "Media hashing exceeded its deadline"
                    )
            after = os.fstat(source.fileno())
        final = path.stat()
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(
            getattr(before, name) != getattr(after, name)
            or getattr(after, name) != getattr(final, name)
            for name in fields
        ):
            raise VideoGenieError("INPUT_CHANGED", "Media changed while hashing it")
        return Fingerprint(
            sha256=digest.hexdigest(),
            size=after.st_size,
            device=after.st_dev,
            inode=after.st_ino,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
        )
    except OSError as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Media file cannot be read") from exc


def require_unchanged(path: Path, expected: Fingerprint) -> None:
    """Cheap final publication guard after the worker has already verified full content."""
    try:
        current = path.stat()
    except OSError as exc:
        raise VideoGenieError("INPUT_CHANGED", "Media disappeared before publication") from exc
    if (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ) != (expected.device, expected.inode, expected.size, expected.mtime_ns, expected.ctime_ns):
        raise VideoGenieError("INPUT_CHANGED", "Media changed before publication")


def _wait_for_exit(process: subprocess.Popen[bytes], deadline: float) -> None:
    """On POSIX retain the leader PID until its process group has been signalled."""
    if os.name != "posix":
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return
    if sys.platform == "darwin":
        # Python 3.12 has no waitid on macOS. kqueue observes exit without
        # consuming the child's wait status or releasing its PID for reuse.
        with closing(select.kqueue()) as queue:
            event = select.kevent(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            events = queue.control([event], 1, max(0.0, deadline - time.monotonic()))
        if not events:
            raise subprocess.TimeoutExpired(process.args, 0)
        if events[0].flags & select.KQ_EV_ERROR and events[0].data != errno.ESRCH:
            raise OSError(events[0].data, "Cannot observe probe exit")
        # ESRCH means this still-unreaped owned child exited before registration.
        return
    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, 0)
        time.sleep(min(remaining, 0.01))


def _cleanup_process(process: subprocess.Popen[bytes]) -> VideoGenieError | None:
    """Signal before reaping; bound direct-child cleanup and expose unresolved errors."""
    problems: dict[str, JsonValue] = {}
    group_denied = False
    try:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                group_denied = True
                problems["group_signal_errno"] = error.errno
        if os.name != "posix" or group_denied:
            try:
                # Popen checks its owned child, including an already-exited leader.
                process.kill()
            except OSError as error:
                problems["child_signal_errno"] = error.errno
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            problems["child_reap"] = "deadline_exceeded"
        except OSError as error:
            problems["child_wait_errno"] = error.errno
        if group_denied and process.returncode is not None:
            # Darwin can deny killpg for a zombie-only group. After reaping, only
            # inspect group existence: never signal a possibly reused group ID.
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                if problems.get("group_signal_errno") == errno.EPERM:
                    del problems["group_signal_errno"]
            except OSError:
                problems["group_cleanup"] = "unconfirmed"
            else:
                problems["group_cleanup"] = "unconfirmed"
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError as error:
                    problems["pipe_close_errno"] = error.errno
    if problems:
        return VideoGenieError(
            "MEDIA_UNAVAILABLE", "ffprobe cleanup could not be confirmed", details=problems
        )
    return None


def bounded_process(argv: list[str], timeout_seconds: float) -> bytes:
    """Capture at most 1 MiB across both pipes and kill the child group on timeout."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise VideoGenieError("INVALID_ARGUMENT", "Probe timeout must be positive and finite")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "ffprobe cannot be executed") from exc
    output = bytearray()
    total = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, True)
            selector.register(process.stderr, selectors.EVENT_READ, False)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe exceeded its deadline")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        raise VideoGenieError(
                            "MEDIA_UNAVAILABLE", "ffprobe exceeded its output limit"
                        )
                    if key.data:
                        output.extend(chunk)
            _wait_for_exit(process, deadline)
    except subprocess.TimeoutExpired as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe exceeded its deadline") from exc
    finally:
        primary = sys.exception()
        cleanup = _cleanup_process(process)
        if cleanup is not None:
            if primary is None:
                raise cleanup
            primary.add_note(cleanup.message)
            if isinstance(primary, VideoGenieError):
                primary.details["cleanup"] = cleanup.details
    if process.returncode != 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe could not read valid media")
    return bytes(output)


def probe_tool(ffprobe_path: str | None = None) -> tuple[str, str]:
    """Resolve the installed tool and report its actual bounded version string."""
    executable = shutil.which(ffprobe_path or "ffprobe")
    if executable is None:
        raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "ffprobe is not installed or executable")
    try:
        version = bounded_process([executable, "-version"], 5.0).decode("utf-8").splitlines()[0]
        if not version.startswith("ffprobe version ") or len(version) > 500:
            raise ValueError("unexpected version")
    except (UnicodeError, IndexError, ValueError, VideoGenieError) as exc:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE", "ffprobe version could not be verified"
        ) from exc
    return str(Path(executable).resolve()), version


def _rational(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ratio = Fraction(value)
        return f"{ratio.numerator}/{ratio.denominator}"
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(
    path: Path,
    timeout_seconds: float,
    ffprobe_path: str,
    *,
    expected: Fingerprint,
    tool_version: str,
) -> ProbeResult:
    """Run actual ffprobe and reject metadata if the source digest changed meanwhile."""
    if fingerprint(path, timeout_seconds) != expected:
        raise VideoGenieError("INPUT_CHANGED", "Media content changed before execution")
    raw = bounded_process(
        [
            ffprobe_path,
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-show_entries",
            "format=format_name,duration:stream=index,codec_type,codec_name,duration,"
            "avg_frame_rate,sample_rate,time_base,width,height,channels,nb_frames",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds,
    )
    if fingerprint(path, timeout_seconds) != expected:
        raise VideoGenieError("INPUT_CHANGED", "Media content changed during execution")
    try:
        parsed = _RawProbe.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "ffprobe returned invalid metadata") from exc
    return ProbeResult(
        input_path=str(path),
        input_sha256=expected.sha256,
        input_size=expected.size,
        ffprobe_version=tool_version,
        format_name=parsed.format.format_name,
        duration=_rational(parsed.format.duration),
        streams=tuple(
            ProbeStream(
                index=stream.index,
                codec_type=stream.codec_type,
                codec_name=stream.codec_name,
                duration=_rational(stream.duration),
                frame_rate=_rational(stream.avg_frame_rate),
                sample_rate=_rational(stream.sample_rate),
                time_base=_rational(stream.time_base),
                width=stream.width,
                height=stream.height,
                channels=stream.channels,
                nb_frames=int(stream.nb_frames)
                if stream.nb_frames is not None and stream.nb_frames.isdecimal()
                else None,
            )
            for stream in parsed.streams
        ),
    )
