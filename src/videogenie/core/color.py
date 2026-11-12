"""Turn looks and adjustments into CDL values and apply them in Resolve color versions."""

from typing import Any

from videogenie.adapters import resolve
from videogenie.contracts.color import AppliedColor, ApplyColorInput, CDLValues, GradedItem, Look
from videogenie.contracts.models import VideoGenieError
from videogenie.core.resolve_render import _timeline
from videogenie.core.service import VideoGenieService

Triple = tuple[float, float, float]
LOOKS: dict[Look, tuple[Triple, Triple, Triple, float]] = {
    "neutral": ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), 1.0),
    "neutral_punch": ((1.04, 1.04, 1.04), (-0.01, -0.01, -0.01), (1.08, 1.08, 1.08), 1.12),
    "warm_cinematic": ((1.06, 1.0, 0.92), (0.005, 0.0, -0.01), (1.06, 1.06, 1.06), 0.92),
    "cool_clean": ((0.97, 1.0, 1.06), (0.0, 0.0, 0.005), (1.03, 1.03, 1.03), 1.0),
    "soft_film": ((0.98, 0.98, 0.98), (0.02, 0.02, 0.02), (0.95, 0.95, 0.95), 0.9),
    "mono": ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0), (1.05, 1.05, 1.05), 0.0),
}


def cdl_values(request: ApplyColorInput) -> CDLValues:
    """Combine the look with adjustments; every value stays inside Resolve's CDL range."""
    slope, offset, power, saturation = LOOKS[request.look]
    adjust = request.adjustments
    gain = 2**adjust.exposure_stops
    balance = (1 + 0.08 * adjust.temperature, 1 - 0.06 * adjust.tint, 1 - 0.08 * adjust.temperature)
    new_slope = tuple(round(s * gain * b, 4) for s, b in zip(slope, balance, strict=True))
    new_power = tuple(round(p * adjust.contrast, 4) for p in power)
    return CDLValues(
        slope=new_slope,  # type: ignore[arg-type]
        offset=offset,
        power=new_power,  # type: ignore[arg-type]
        saturation=round(saturation * adjust.saturation, 4),
    )


def _items(timeline: Any, request: ApplyColorInput) -> list[tuple[int, Any]]:
    clips = sorted(
        timeline.GetItemListInTrack("video", request.track_index) or [],
        key=lambda item: item.GetStart(),
    )
    numbered = list(enumerate(clips, start=1))
    if request.item_numbers is None:
        selected = numbered
    else:
        wanted = set(request.item_numbers)
        selected = [(number, item) for number, item in numbered if number in wanted]
        if len(selected) != len(wanted):
            raise VideoGenieError("NOT_FOUND", "Some item_numbers are not on that track")
    if not selected:
        raise VideoGenieError("NOT_FOUND", "No clips on that track to grade")
    return selected


def apply_color(service: VideoGenieService, request: ApplyColorInput) -> AppliedColor:
    """Apply the grade to each selected clip inside its own color version."""
    service.get_project(request.project_id)
    cdl = cdl_values(request)
    app = resolve.connect(30.0)
    project = resolve.open_project(app, request.project_id, allow_switch=request.allow_switch)
    timeline = _timeline(project, request.timeline_name)
    project.SetCurrentTimeline(timeline)
    graded: list[GradedItem] = []
    for number, item in _items(timeline, request):
        if request.version_name not in (item.GetVersionNameList(0) or []):
            if not item.AddVersion(request.version_name, 0):
                raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not add a version")
        if not item.LoadVersionByName(request.version_name, 0):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve could not load the version")
        values = {
            "NodeIndex": 1,
            "Slope": " ".join(f"{value:g}" for value in cdl.slope),
            "Offset": " ".join(f"{value:g}" for value in cdl.offset),
            "Power": " ".join(f"{value:g}" for value in cdl.power),
            "Saturation": cdl.saturation,
        }
        if not item.SetCDL(values):
            raise VideoGenieError("DEPENDENCY_UNAVAILABLE", "Resolve rejected the CDL values")
        if request.lut is not None and not item.GetNodeGraph().SetLUT(1, request.lut):
            raise VideoGenieError("NOT_FOUND", "That LUT is not installed in Resolve's LUT library")
        graded.append(
            GradedItem(
                item_number=number, start_frame=int(item.GetStart()), name=str(item.GetName())
            )
        )
    return AppliedColor(
        timeline_name=request.timeline_name,
        version_name=request.version_name,
        look=request.look,
        cdl=cdl,
        lut=request.lut,
        items=tuple(graded),
    )
