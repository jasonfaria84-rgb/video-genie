"""Plan transcript-based cuts for filler words, false starts and long pauses.

Planning is pure arithmetic on stored transcript timings in whole milliseconds. Kept
words are widened to whole milliseconds before planning, so a cut can never start or
end inside one. Each gap between kept words yields at most one cut, so a filler, a
false start and the pause around them merge into one cut with the most specific reason.
"""

import math
from datetime import UTC, datetime
from uuid import uuid4

from videogenie.contracts.edit import PlanCutsInput, normalize_token
from videogenie.contracts.models import VideoGenieError
from videogenie.contracts.transcripts import (
    CutPlan,
    CutReason,
    KeptRange,
    PlannedCut,
    Transcript,
    TranscriptWord,
)
from videogenie.core import transcripts as store
from videogenie.core.service import VideoGenieService

FALSE_START_MAX_GAP_SECONDS = 1.0
MAX_FALSE_START_WORDS = 4
MIN_SILENCE_CUT_MS = 50
MAX_PLAN_RANGES = 20_000
# Single words people double on purpose ("very very", "that that"); never false starts.
INTENTIONAL_REPEATS = frozenset(
    {"very", "really", "so", "no", "yes", "yeah", "bye", "ha", "that", "had", "please", "okay"}
)
_PRIORITY: dict[CutReason, int] = {"filler": 0, "false_start": 1}


def _filler_sequences(filler_words: tuple[str, ...]) -> list[tuple[str, ...]]:
    sequences = {
        tuple(token for token in (normalize_token(part) for part in phrase.split()) if token)
        for phrase in filler_words
    }
    return sorted((seq for seq in sequences if seq), key=lambda seq: (-len(seq), seq))


def _mark_fillers(
    tokens: list[str], spoken: list[int], fillers: list[tuple[str, ...]]
) -> dict[int, CutReason]:
    marked: dict[int, CutReason] = {}
    position = 0
    while position < len(spoken):
        for sequence in fillers:
            window = spoken[position : position + len(sequence)]
            if tuple(tokens[index] for index in window) == sequence:
                marked.update(dict.fromkeys(window, "filler"))
                position += len(sequence)
                break
        else:
            position += 1
    return marked


def _mark_false_starts(
    words: list[TranscriptWord], tokens: list[str], spoken: list[int]
) -> dict[int, CutReason]:
    """Cut the earlier copy of a word or short phrase that is immediately repeated."""
    marked: dict[int, CutReason] = {}
    position = 0
    while position < len(spoken):
        for size in range(MAX_FALSE_START_WORDS, 0, -1):
            first = spoken[position : position + size]
            second = spoken[position + size : position + 2 * size]
            if len(second) < size:
                continue
            phrase = [tokens[index] for index in first]
            if phrase != [tokens[index] for index in second] or "" in phrase:
                continue
            if size == 1 and phrase[0] in INTENTIONAL_REPEATS:
                continue
            gap = words[second[0]].start_seconds - words[first[-1]].end_seconds
            if gap > FALSE_START_MAX_GAP_SECONDS:
                continue
            if len({words[index].speaker for index in first + second} - {None}) > 1:
                continue
            marked.update(dict.fromkeys(first, "false_start"))
            position += size
            break
        else:
            position += 1
    return marked


def plan_ranges(
    words: tuple[TranscriptWord, ...], duration_seconds: float, request: PlanCutsInput
) -> tuple[tuple[PlannedCut, ...], tuple[KeptRange, ...]]:
    """Return cuts and the kept ranges between them for time-ordered transcript words."""
    total = math.floor(duration_seconds * 1000)
    padding = round(request.padding_seconds * 1000)
    min_pause = round(request.min_pause_seconds * 1000)

    def seconds(ms: int) -> float:
        return duration_seconds if ms >= total else ms / 1000

    # Silence markers are not content; spoken words and audible sound are.
    content = [word for word in words if word.kind != "silence"]
    spans = [
        (
            min(math.floor(word.start_seconds * 1000), total),
            min(math.ceil(word.end_seconds * 1000), total),
        )
        for word in content
    ]
    tokens = [normalize_token(word.text) if word.kind == "word" else "" for word in content]
    spoken = [index for index, word in enumerate(content) if word.kind == "word"]
    removed = _mark_fillers(tokens, spoken, _filler_sequences(request.filler_words))
    for index, word in enumerate(content):
        if word.kind == "sound" and spans[index][1] - spans[index][0] <= round(
            request.max_filler_seconds * 1000
        ):
            removed[index] = "filler"
    if request.remove_false_starts:
        remaining = [index for index in spoken if index not in removed]
        removed.update(_mark_false_starts(content, tokens, remaining))

    # A removed word overlapping kept content cannot be cut cleanly, so it is kept.
    changed = True
    while changed:
        changed = False
        kept_before = 0
        kept_after = [total] * (len(content) + 1)
        for index in range(len(content) - 1, -1, -1):
            kept_after[index] = kept_after[index + 1] if index in removed else spans[index][0]
        for index, (start, end) in enumerate(spans):
            if index in removed and (start < kept_before or end > kept_after[index + 1]):
                del removed[index]
                changed = True
            if index not in removed:
                kept_before = max(kept_before, end)

    if len(removed) == len(content):
        kept = (KeptRange(start_seconds=0.0, end_seconds=duration_seconds),) if total else ()
        return (), kept

    cuts: list[tuple[int, int, CutReason, tuple[str, ...]]] = []
    group: list[int] = []
    reach: int | None = None
    for index in [*range(len(content)), None]:
        if index is not None and index in removed:
            group.append(index)
            continue
        low = 0 if reach is None else reach
        high = total if index is None else spans[index][0]
        leading, trailing = reach is None, index is None
        if high > low and group:
            first = min(spans[item][0] for item in group)
            last = max(spans[item][1] for item in group)
            start = 0 if leading else max(low, min(low + padding, first))
            end = total if trailing else min(high, max(high - padding, last))
            reason = min((removed[item] for item in group), key=_PRIORITY.__getitem__)
            ids = tuple(content[item].word_id for item in group)
            if end > start:
                cuts.append((start, end, reason, ids))
        elif high - low >= min_pause:
            start = 0 if leading else low + padding
            end = total if trailing else high - padding
            silence: CutReason = (
                "leading_silence" if leading else "trailing_silence" if trailing else "pause"
            )
            if end - start >= MIN_SILENCE_CUT_MS:
                cuts.append((start, end, silence, ()))
        group = []
        if index is not None:
            reach = spans[index][1] if reach is None else max(reach, spans[index][1])
    if len(cuts) >= MAX_PLAN_RANGES:
        raise VideoGenieError(
            "INVALID_ARGUMENT", "Too many cuts for one plan; raise min_pause_seconds"
        )

    kept_ranges: list[KeptRange] = []
    position = 0
    for start, end, _, _ in cuts:
        if start > position:
            kept_ranges.append(KeptRange(start_seconds=seconds(position), end_seconds=start / 1000))
        position = end
    if total > position:
        kept_ranges.append(KeptRange(start_seconds=seconds(position), end_seconds=duration_seconds))
    planned = tuple(
        PlannedCut(
            start_seconds=start / 1000, end_seconds=seconds(end), reason=reason, word_ids=ids
        )
        for start, end, reason, ids in cuts
    )
    return planned, tuple(kept_ranges)


def plan_cuts(transcript: Transcript, request: PlanCutsInput) -> CutPlan:
    """Build a validated cut plan for one transcript without saving it."""
    cuts, kept = plan_ranges(transcript.words, transcript.duration_seconds, request)
    return CutPlan(
        plan_id=str(uuid4()),
        transcript_id=transcript.transcript_id,
        source_sha256=transcript.source_sha256,
        duration_seconds=transcript.duration_seconds,
        cuts=cuts,
        kept=kept,
        created_at=datetime.now(UTC).isoformat(),
    )


def create_cut_plan(service: VideoGenieService, request: PlanCutsInput) -> CutPlan:
    """Load the transcript, plan its cuts, and save the new plan."""
    transcript = store.load(service, Transcript, request.transcript_id)
    plan = plan_cuts(transcript, request)
    store.save(service, plan)
    return plan
