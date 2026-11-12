"""Write SRT subtitles from a stored transcript and optionally burn them in with FFmpeg.

Inputs are only ever read. Outputs are reserved exclusively, written to a partial file
and moved into place, so an existing file is never replaced; failures remove our own
outputs, including the SRT when a requested burn-in fails.
"""

import hashlib
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from videogenie.contracts.captions import (
    MAX_PREVIEW_CUES,
    BurnInInput,
    BurnInOutput,
    CaptionCue,
    CreateCaptionsInput,
    CreateCaptionsResult,
)
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import CutPlan, Transcript
from videogenie.core import transcripts
from videogenie.core.captions import Cue, build_cues, to_srt
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import (
    absolute_media_path,
    bounded_process,
    fingerprint,
    require_unchanged,
)
from videogenie.workers.silence import _media, _release, _reserve, _tool

VIDEO_SUFFIXES = {".mp4", ".mov"}
# Seconds a last cue may run past the video before we assume the wrong video was given.
END_TOLERANCE_SECONDS = 0.5


def force_style(font_size: int) -> str:
    """libass style: bold white text, black outline, bottom centre (288-line canvas)."""
    return (
        f"FontSize={font_size},Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        "BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=24"
    )


def filter_value(value: str) -> str:
    """Escape an option value for an FFmpeg filtergraph passed as one argv string.

    Level one protects the option value (backslash, quote, colon); level two protects
    the graph (backslash, quote, brackets, comma, semicolon).
    """
    value = value.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    value = value.replace("\\", "\\\\").replace("'", "\\'")
    for special in "[],;":
        value = value.replace(special, "\\" + special)
    return value


def _output_path(value: str, suffixes: set[str], taken: set[Path]) -> Path:
    try:
        candidate = Path(value)
        if not candidate.is_absolute() or any(ord(char) < 32 for char in value):
            raise ValueError("relative")
        parent = candidate.parent.resolve(strict=True)
    except (OSError, ValueError, UnicodeError) as exc:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "Output paths must be absolute paths in existing folders"
        ) from exc
    output = parent / candidate.name
    if output.suffix.lower() not in suffixes:
        raise VideoGenieError(
            "INVALID_ARGUMENT", f"Output must end in {' or '.join(sorted(suffixes))}"
        )
    if output in taken or os.path.lexists(output):
        raise VideoGenieError("INVALID_ARGUMENT", f"{output.name} already exists or is reused")
    return output


def _deadline_left(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "Subtitle burn-in exceeded its deadline")
    return remaining


def _require_subtitles_filter(ffmpeg: str, deadline: float) -> None:
    raw = bounded_process(
        [ffmpeg, "-hide_banner", "-filters"], min(10.0, _deadline_left(deadline)), tool="ffmpeg"
    )
    names = {
        line.split()[1]
        for line in raw.decode("utf-8", "replace").splitlines()[1:]
        if len(line.split()) > 2
    }
    if "subtitles" not in names:
        raise VideoGenieError(
            "DEPENDENCY_UNAVAILABLE",
            "This FFmpeg has no subtitles filter (built without libass); install an FFmpeg "
            "with libass to burn in, or omit burn_in and import the SRT instead",
        )


def _publish_srt(output: Path, text: str) -> tuple[int, int]:
    """Write the SRT through an exclusive reservation; returns the published file identity."""
    reserved = _reserve(output)
    partial = output.with_name(f".{output.stem}.{uuid4().hex}.partial.srt")
    try:
        with partial.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        current = output.stat()
        if (current.st_dev, current.st_ino) != reserved or current.st_size != 0:
            raise VideoGenieError("INPUT_CHANGED", "output_path changed while writing")
        os.replace(partial, output)
        published = output.stat()
        return published.st_dev, published.st_ino
    except OSError as exc:
        _release(output, reserved)
        raise VideoGenieError("ACCESS_DENIED", "Subtitle file could not be written") from exc
    except BaseException:
        _release(output, reserved)
        raise
    finally:
        partial.unlink(missing_ok=True)


def _unpublish(output: Path, identity: tuple[int, int]) -> None:
    """Remove a file we published, only while it is still that same file."""
    try:
        stats = output.stat()
        if (stats.st_dev, stats.st_ino) == identity:
            output.unlink()
    except OSError:
        pass


def _burn(
    request: BurnInInput,
    video: Path,
    output: Path,
    srt_text: str,
    cues: list[Cue],
    ffmpeg: str,
    deadline: float,
) -> BurnInOutput:
    source = fingerprint(video, _deadline_left(deadline))
    ffprobe, _ = _tool("ffprobe", deadline)
    media = _media(ffprobe, video, deadline)
    if not media.has_video:
        raise VideoGenieError("MEDIA_UNAVAILABLE", "burn_in.video_path has no video stream")
    if cues and cues[-1].end_ms / 1000 > media.duration + END_TOLERANCE_SECONDS:
        raise VideoGenieError(
            "INVALID_ARGUMENT",
            "Subtitles run past the end of burn_in.video_path; with a cut plan, pass the "
            "edited output rather than the source",
            details={
                "last_cue_end_seconds": cues[-1].end_ms / 1000,
                "video_seconds": round(media.duration, 3),
            },
        )
    style = force_style(request.font_size)
    reserved = _reserve(output)
    partial = output.with_name(f".{output.stem}.{uuid4().hex}.partial{output.suffix}")
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix="videogenie-captions-") as folder:
            # A fixed safe name keeps the filter argument simple; it is still escaped.
            subtitles = Path(folder) / "captions.srt"
            subtitles.write_text(srt_text, encoding="utf-8", newline="\n")
            graph = (
                f"subtitles=filename={filter_value(str(subtitles))}"
                f":force_style={filter_value(style)}"
            )
            bounded_process(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-nostdin",
                    "-nostats",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-i",
                    str(video),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0?",
                    "-vf",
                    graph,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "18",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    "-n",
                    str(partial),
                ],
                _deadline_left(deadline),
                tool="ffmpeg",
            )
        require_unchanged(video, source)
        measured = _media(ffprobe, partial, deadline)
        written = fingerprint(partial, _deadline_left(deadline))
        current = output.stat()
        if (current.st_dev, current.st_ino) != reserved or current.st_size != 0:
            raise VideoGenieError("INPUT_CHANGED", "burn_in.output_path changed during the render")
        os.replace(partial, output)
        published = True
    except OSError as exc:
        raise VideoGenieError("ACCESS_DENIED", "Subtitled video could not be written") from exc
    finally:
        if not published:
            _release(output, reserved)
        partial.unlink(missing_ok=True)
    return BurnInOutput(
        path=str(output),
        sha256=written.sha256,
        size=written.size,
        duration_seconds=round(measured.duration, 3),
        video_sha256=source.sha256,
        font_size=request.font_size,
        force_style=style,
    )


def create_captions(
    service: VideoGenieService, request: CreateCaptionsInput
) -> CreateCaptionsResult:
    """Build cues, write the SRT, then burn in when asked; validation precedes any write."""
    transcript = transcripts.load(service, Transcript, request.transcript_id)
    plan: CutPlan | None = None
    if request.cut_plan_id is not None:
        plan = transcripts.load(service, CutPlan, request.cut_plan_id)
        if (
            plan.transcript_id != transcript.transcript_id
            or plan.source_sha256 != transcript.source_sha256
        ):
            raise VideoGenieError(
                "INVALID_ARGUMENT", "cut_plan_id was not made from this transcript"
            )
    srt_output = _output_path(request.output_path, {".srt"}, set())
    burn = request.burn_in
    deadline = time.monotonic() + (burn.timeout_seconds if burn else 0)
    video: Path | None = None
    video_output: Path | None = None
    ffmpeg = ""
    if burn is not None:
        video = absolute_media_path(burn.video_path)
        video_output = _output_path(burn.output_path, VIDEO_SUFFIXES, {video, srt_output})
        ffmpeg, _ = _tool("ffmpeg", deadline)
        _require_subtitles_filter(ffmpeg, deadline)
    cues, dropped = build_cues(
        transcript,
        plan,
        words_per_cue=request.words_per_cue,
        max_cue_seconds=request.max_cue_seconds,
        min_cue_seconds=request.min_cue_seconds,
        uppercase=request.uppercase,
    )
    if not cues:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "No spoken words remain to caption; nothing was written"
        )
    text = to_srt(cues)
    identity = _publish_srt(srt_output, text)
    burned: BurnInOutput | None = None
    if burn is not None and video is not None and video_output is not None:
        try:
            burned = _burn(burn, video, video_output, text, cues, ffmpeg, deadline)
        except BaseException:
            _unpublish(srt_output, identity)
            raise
    encoded = text.encode("utf-8")
    return CreateCaptionsResult(
        transcript_id=transcript.transcript_id,
        cut_plan_id=None if plan is None else plan.plan_id,
        cue_count=len(cues),
        words_captioned=sum(len(cue.words) for cue in cues),
        words_dropped_in_cuts=dropped,
        srt_path=str(srt_output),
        srt_sha256=hashlib.sha256(encoded).hexdigest(),
        srt_size=len(encoded),
        preview=tuple(
            CaptionCue(
                index=number,
                start_seconds=cue.start_ms / 1000,
                end_seconds=cue.end_ms / 1000,
                text=cue.text,
            )
            for number, cue in enumerate(cues[:MAX_PREVIEW_CUES], start=1)
        ),
        burn_in=burned,
    )
