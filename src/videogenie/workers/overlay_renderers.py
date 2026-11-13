"""Detect motion-graphics renderers without installing, downloading or rendering anything."""

import shutil
import time

import PIL

from videogenie.adapters import resolve
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.overlays import OverlayRenderers, RendererName, RendererStatus
from videogenie.workers.probe import bounded_process

VERSION_SECONDS = 5.0
FIXES = {
    "ffmpeg": "Install FFmpeg so ffmpeg is on PATH",
    "ffprobe": "Install FFmpeg so ffprobe is on PATH",
    "node": "Install Node.js 18 or newer so node is on PATH",
    "remotion": "Install the Remotion CLI (@remotion/cli) so remotion is on PATH",
    "hyperframes": "Install the HyperFrames CLI so hyperframes is on PATH",
    "manim": "Install Manim Community Edition so manim is on PATH",
}
NOTES: dict[RendererName, str] = {
    "pil": "Renders lower thirds as qtrle alpha MOVs (graphics.render_lower_third).",
    "fusion": "Fusion runs inside Resolve; overlay.place attaches .comp files to a clip.",
    "lottie": "Native Lottie import needs Resolve 21 or newer and is unverified; placing Lottie "
    "files is not yet supported.",
    "remotion": "Detection only. Remotion's license is free only for individuals and companies "
    "of 1-3 people; larger companies need a paid license. Off by default until the owner "
    "decides. Rendering also needs Chrome Headless Shell from a pinned template.",
    "hyperframes": "Detection only; rendering is not yet supported.",
    "manim": "Detection only; rendering is not yet supported. Tex/MathTex scenes also need "
    "latex and dvisvgm.",
}


def _version(name: str, deadline: float) -> str | None:
    """First line of `<tool> --version` (or -version for FFmpeg), or None if it doesn't run."""
    executable = shutil.which(name)
    remaining = deadline - time.monotonic()
    if executable is None or remaining <= 0:
        return None
    flag = "-version" if name in {"ffmpeg", "ffprobe"} else "--version"
    try:
        raw = bounded_process([executable, flag], min(VERSION_SECONDS, remaining), tool=name)
        lines = raw.decode("utf-8", errors="replace").strip().splitlines()
    except VideoGenieError:
        return None
    return lines[0].strip()[:200] if lines and lines[0].strip() else None


def _tools(
    renderer: RendererName,
    needed: tuple[str, ...],
    deadline: float,
    seen: dict[str, str | None],
) -> RendererStatus:
    """The last needed tool names the renderer; its version is the renderer's version."""
    for name in needed:
        if name not in seen:
            seen[name] = _version(name, deadline)
    versions = {name: seen[name] for name in needed}
    missing = tuple(name for name in needed if versions[name] is None)
    return RendererStatus(
        renderer=renderer,
        available=not missing,
        version=versions[needed[-1]],
        missing=missing,
        fix="; ".join(FIXES[name] for name in missing) or None,
        rendering_supported=False,
        note=NOTES[renderer],
    )


def _major(version: str | None) -> int:
    head = (version or "").split(".")[0]
    return int(head) if head.isdecimal() else 0


def detect_renderers(timeout_seconds: float) -> OverlayRenderers:
    """Check every renderer within one overall deadline; nothing is installed or changed."""
    deadline = time.monotonic() + timeout_seconds
    seen: dict[str, str | None] = {}
    pil = _tools("pil", ("ffprobe", "ffmpeg"), deadline, seen)
    pil = pil.model_copy(
        update={"version": f"Pillow {PIL.__version__}", "rendering_supported": pil.available}
    )
    status = resolve.resolve_status(max(0.5, min(10.0, deadline - time.monotonic())))
    studio = status.reachable and status.studio
    fusion = RendererStatus(
        renderer="fusion",
        available=studio,
        version=status.version,
        missing=() if studio else ("davinci-resolve-studio",),
        fix=status.fix,
        rendering_supported=studio,
        note=NOTES["fusion"],
    )
    lottie_ready = studio and _major(status.version) >= 21
    lottie = fusion.model_copy(
        update={
            "renderer": "lottie",
            "available": lottie_ready,
            "missing": () if lottie_ready else ("davinci-resolve-studio-21",),
            "fix": None if lottie_ready else status.fix or "Update to DaVinci Resolve Studio 21",
            "rendering_supported": False,
            "note": NOTES["lottie"],
        }
    )
    manim = _tools("manim", ("manim",), deadline, seen)
    latex = all(shutil.which(name) for name in ("latex", "dvisvgm"))
    manim = manim.model_copy(
        update={"note": f"{NOTES['manim']} LaTeX {'found' if latex else 'not found'}."}
    )
    return OverlayRenderers(
        renderers=(
            pil,
            fusion,
            lottie,
            _tools("remotion", ("node", "remotion"), deadline, seen),
            _tools("hyperframes", ("node", "hyperframes"), deadline, seen),
            manim,
        )
    )
