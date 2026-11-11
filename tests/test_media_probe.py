"""Real media probing, durable recovery, bounded failures, and worker fencing."""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from videogenie.contracts.models import Project, VideoGenieError
from videogenie.core.jobs import JobService
from videogenie.core.service import VideoGenieService
from videogenie.workers.probe import fingerprint, probe_tool


@pytest.fixture
def media(tmp_path: Path) -> Path:
    """Generate synthetic media with real ffmpeg; successful probes never use fake JSON."""
    path = tmp_path / "source with spaces.wav"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None, "Real ffmpeg is required for media acceptance"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.2",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=10,
    )
    return path


@pytest.fixture
def registry(tmp_path: Path) -> tuple[VideoGenieService, Project]:
    service = VideoGenieService(tmp_path / "state")
    return service, service.create_project("Probe", "project")


def wrapper(tmp_path: Path, body: str) -> str:
    """Make a tool wrapper for deterministic failure injection, preserving version checks."""
    real, _ = probe_tool()
    path = tmp_path / "ffprobe-wrapper"
    path.write_text(
        f"#!{Path(sys.executable).resolve()}\nimport os, sys, time\n"
        f"real = {real!r}\n"
        "if '-version' in sys.argv:\n    os.execv(real, [real, '-version'])\n" + body,
    )
    path.chmod(0o700)
    return str(path)


def test_real_probe_replay_restart_and_artifact(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    before = media.read_bytes()
    jobs = JobService(service)
    job = jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert job.status == "succeeded"
    assert job.input_sha256 == hashlib.sha256(before).hexdigest()
    assert job.result is not None and job.result_ref is not None
    assert job.result.format_name == "wav"
    assert job.result.streams[0].codec_type == "audio"
    assert job.result.streams[0].sample_rate == "44100/1"
    assert job.result.duration == "1/5"
    assert job.result.ffprobe_version == probe_tool()[1]
    assert job.result_ref.sha256 == hashlib.sha256(job.result_ref.payload_json.encode()).hexdigest()
    assert len(job.result_ref.payload_json.encode()) == job.result_ref.byte_size
    assert jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe") == job
    assert len(job.attempts) == 1 and job.attempts[0].status == "succeeded"
    assert media.read_bytes() == before
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from videogenie.core.service import VideoGenieService; "
            "from videogenie.core.jobs import JobService; "
            "print(JobService(VideoGenieService(sys.argv[1])).get_job(sys.argv[2],sys.argv[3])"
            ".model_dump_json())",
            str(service.data_dir),
            project.project_id,
            job.job_id,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    assert json.loads(child.stdout) == job.model_dump(mode="json")
    assert jobs.resume_job(project.project_id, job.job_id) == job


def test_input_and_configuration_conflicts(
    registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    jobs = JobService(service)
    job = jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    with pytest.raises(VideoGenieError) as config:
        JobService(service, timeout_seconds=2).submit_probe(
            project.project_id, project.storyline_id, str(media), "probe"
        )
    assert config.value.code == "IDEMPOTENCY_CONFLICT"
    media.write_bytes(media.read_bytes() + b"different")
    with pytest.raises(VideoGenieError) as changed:
        jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert changed.value.code == "IDEMPOTENCY_CONFLICT"
    assert jobs.get_job(project.project_id, job.job_id) == job


def test_missing_tool_retains_failure_and_can_resume(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    jobs = JobService(service, ffprobe_path=str(tmp_path / "absent"))
    with pytest.raises(VideoGenieError) as error:
        jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    job_id = str(error.value.details["job_id"])
    failed = jobs.get_job(project.project_id, job_id)
    assert failed.status == "needs_input" and failed.failure is not None
    assert failed.attempts[0].status == "failed"
    assert jobs.capabilities()["ffprobe"] == {
        "available": False,
        "path": None,
        "version": None,
        "error": failed.failure.model_dump(mode="json") | {"details": {}},
    }
    recovered = JobService(service).resume_job(project.project_id, job_id)
    assert recovered.status == "succeeded" and len(recovered.attempts) == 2
    assert recovered.attempts[0] == failed.attempts[0]


def test_bad_media_and_missing_input_are_durable(
    tmp_path: Path, registry: tuple[VideoGenieService, Project]
) -> None:
    service, project = registry
    jobs = JobService(service)
    path = tmp_path / "invalid.bin"
    for key in ("missing", "invalid"):
        if key == "invalid":
            path.write_bytes(b"not a media stream")
        with pytest.raises(VideoGenieError) as error:
            jobs.submit_probe(project.project_id, project.storyline_id, str(path), key)
        assert error.value.code == "MEDIA_UNAVAILABLE"
        failed = jobs.get_job(project.project_id, str(error.value.details["job_id"]))
        assert failed.status == "needs_input" and failed.result_ref is None
        assert failed.failure is not None and len(failed.attempts) == 1


def test_project_scope_archive_and_invalid_path(
    registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    jobs = JobService(service)
    other = service.create_project("Other", "other")
    job = jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    with pytest.raises(VideoGenieError) as access:
        jobs.get_job(other.project_id, job.job_id)
    assert access.value.code == "NOT_FOUND"
    with pytest.raises(VideoGenieError):
        jobs.submit_probe(other.project_id, project.storyline_id, str(media), "wrong")
    for path in ("relative.wav", "https://example.com/test.wav", "x\x00y"):
        with pytest.raises(VideoGenieError) as invalid:
            jobs.submit_probe(project.project_id, project.storyline_id, path, "invalid")
        assert invalid.value.code == "INVALID_ARGUMENT"
    service.archive_project(project.project_id, 1)
    assert jobs.get_job(project.project_id, job.job_id) == job
    assert jobs.resume_job(project.project_id, job.job_id) == job
    with pytest.raises(VideoGenieError) as archived:
        jobs.submit_probe(project.project_id, project.storyline_id, str(media), "new")
    assert archived.value.code == "PROJECT_ARCHIVED"


@pytest.mark.parametrize(
    "body",
    [
        "time.sleep(5)\n",
        "print('not-json')\n",
        "print('x' * (1024 * 1024 + 1))\n",
    ],
)
def test_bounded_tool_failures(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path, body: str
) -> None:
    service, project = registry
    jobs = JobService(service, ffprobe_path=wrapper(tmp_path, body), timeout_seconds=0.3)
    start = time.monotonic()
    with pytest.raises(VideoGenieError) as error:
        jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert error.value.code == "MEDIA_UNAVAILABLE"
    assert time.monotonic() - start < 3
    failed = jobs.get_job(project.project_id, str(error.value.details["job_id"]))
    assert failed.status == "needs_input" and failed.result_ref is None


def test_changed_input_during_real_probe_is_not_published(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    tool = wrapper(
        tmp_path,
        "import subprocess\n"
        "result = subprocess.run([real, *sys.argv[1:]], capture_output=True)\n"
        "with open(sys.argv[-1], 'ab') as output: output.write(b'changed')\n"
        "sys.stdout.buffer.write(result.stdout)\n",
    )
    jobs = JobService(service, ffprobe_path=tool)
    with pytest.raises(VideoGenieError) as error:
        jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
    assert error.value.code == "INPUT_CHANGED"
    job = jobs.get_job(project.project_id, str(error.value.details["job_id"]))
    assert job.result_ref is None and job.status == "needs_input"


def test_live_lease_and_stale_worker_cannot_publish(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    started = tmp_path / "started"
    release = tmp_path / "release"
    tool = wrapper(
        tmp_path,
        f"from pathlib import Path\nPath({str(started)!r}).touch()\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "os.execv(real, [real, *sys.argv[1:]])\n",
    )
    jobs = JobService(service, ffprobe_path=tool, lease_seconds=0.4)
    errors: list[VideoGenieError] = []

    def run_old() -> None:
        try:
            jobs.submit_probe(project.project_id, project.storyline_id, str(media), "probe")
        except VideoGenieError as error:
            errors.append(error)

    worker = threading.Thread(target=run_old)
    worker.start()
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started.exists()
    with service.connection() as connection:
        job_id = str(connection.execute("SELECT job_id FROM jobs").fetchone()[0])
    with pytest.raises(VideoGenieError) as held:
        jobs.resume_job(project.project_id, job_id)
    assert held.value.code == "LEASE_HELD"
    time.sleep(0.45)
    replacement = JobService(service).resume_job(project.project_id, job_id)
    release.touch()
    worker.join(5)
    assert not worker.is_alive() and errors[0].code == "LEASE_HELD"
    assert replacement.status == "succeeded" and replacement.fence == 2
    assert replacement.attempts[0].status == "interrupted"
    assert JobService(service).get_job(project.project_id, job_id) == replacement
    with service.connection() as connection:
        assert connection.execute("SELECT count(*) FROM job_artifacts").fetchone()[0] == 1


def test_killed_process_recovers_durable_attempt(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], media: Path
) -> None:
    service, project = registry
    barrier = tmp_path / "claimed"
    tool = wrapper(
        tmp_path,
        f"from pathlib import Path\nPath({str(barrier)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(10)\n",
    )
    script = (
        "import sys; from videogenie.core.service import VideoGenieService; "
        "from videogenie.core.jobs import JobService; "
        "JobService(VideoGenieService(sys.argv[1]),ffprobe_path=sys.argv[2],lease_seconds=0.5)"
        ".submit_probe(sys.argv[3],sys.argv[4],sys.argv[5],'interrupt')"
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(service.data_dir),
            tool,
            project.project_id,
            project.storyline_id,
            str(media),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not barrier.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert barrier.exists()
        child.kill()
        child.communicate(timeout=5)
        os.killpg(int(barrier.read_text()), signal.SIGKILL)
        with service.connection() as connection:
            job_id = str(connection.execute("SELECT job_id FROM jobs").fetchone()[0])
        interrupted = JobService(service).get_job(project.project_id, job_id)
        assert interrupted.status == "running" and len(interrupted.attempts) == 1
        time.sleep(0.55)
        recovered = JobService(service).resume_job(project.project_id, job_id)
        assert recovered.status == "succeeded" and len(recovered.attempts) == 2
        assert recovered.attempts[0].status == "interrupted"
        assert recovered.input_sha256 == fingerprint(media).sha256
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("with_audio", [True, False])
def test_real_video_geometry_timing_and_optional_audio(
    tmp_path: Path, registry: tuple[VideoGenieService, Project], with_audio: bool
) -> None:
    """Exercise actual AVC video plus optional AAC and compare ffprobe's reported facts."""
    service, project = registry
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    path = tmp_path / "synthetic-video.mp4"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=64x48:r=24:d=0.25"]
    if with_audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=0.25"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio:
        command += ["-c:a", "aac"]
    command += ["-t", "0.25", "-y", str(path)]
    subprocess.run(command, check=True, capture_output=True, timeout=10)
    source_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    reference = subprocess.run(
        [probe_tool()[0], "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    actual: dict[str, list[dict[str, object]]] = json.loads(reference.stdout)
    job = JobService(service).submit_probe(
        project.project_id, project.storyline_id, str(path), "video"
    )
    assert job.status == "succeeded" and job.result is not None
    assert job.input_sha256 == source_digest
    assert "mp4" in job.result.format_name.split(",")
    video = next(stream for stream in job.result.streams if stream.codec_type == "video")
    reference_video = next(
        stream for stream in actual["streams"] if stream["codec_type"] == "video"
    )
    assert video.width == reference_video["width"] == 64
    assert video.height == reference_video["height"] == 48
    assert video.codec_name == "h264"
    assert video.frame_rate == reference_video["avg_frame_rate"] == "24/1"
    assert video.nb_frames == int(str(reference_video["nb_frames"])) == 6
    assert video.channels is None and video.sample_rate is None
    audio = [stream for stream in job.result.streams if stream.codec_type == "audio"]
    assert len(audio) == int(with_audio)
    if with_audio:
        reference_audio = next(
            stream for stream in actual["streams"] if stream["codec_type"] == "audio"
        )
        assert audio[0].codec_name == "aac"
        assert audio[0].channels == reference_audio["channels"] == 1
        assert audio[0].sample_rate == f"{reference_audio['sample_rate']}/1" == "48000/1"
        assert audio[0].width is None and audio[0].height is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == source_digest
