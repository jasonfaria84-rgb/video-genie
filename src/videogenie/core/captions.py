"""Pure subtitle chunking: transcript words to short cues in output time, and SRT text.

All timing works in integer milliseconds so the same input always yields the same file.
"""

from dataclasses import dataclass, replace

from videogenie.contracts.transcripts import CutPlan, Transcript, output_time

# Stripped from either end of a word; apostrophes are kept ("don't", "'cause").
STRIP = '.,!?;:"“”„«»()[]{}<>…—–-¿¡*_/\\|~'
# A pause this long ends a cue, so text never appears long before it is spoken.
PAUSE_MS = 500


@dataclass(frozen=True)
class Cue:
    """Subtitle text shown from start_ms to end_ms within one kept segment."""

    start_ms: int
    end_ms: int
    words: tuple[str, ...]
    segment: int
    segment_end_ms: int

    @property
    def text(self) -> str:
        return " ".join(self.words)


def clean_word(text: str) -> str:
    """Remove surrounding punctuation, keeping apostrophes and an abbreviation's last dot."""
    trimmed = text.strip().lstrip(STRIP)
    body = trimmed.rstrip(STRIP)
    if "." in body and trimmed[len(body) : len(body) + 1] == ".":
        body += "."
    return body


def _ms(seconds: float) -> int:
    return round(seconds * 1000)


def timed_words(transcript: Transcript, plan: CutPlan | None) -> tuple[list[Cue], int]:
    """Spoken words as one-word cues in output time, and how many fell inside cuts.

    With a plan, a word belongs to the kept range holding its midpoint and is clipped to
    it; a word whose midpoint is cut is dropped.
    """
    kept = sorted(plan.kept, key=lambda item: item.start_seconds) if plan else []
    words: list[Cue] = []
    dropped = 0
    for word in transcript.words:
        text = clean_word(word.text)
        if word.kind != "word" or not text:
            continue
        start, end, segment = word.start_seconds, word.end_seconds, 0
        segment_end = transcript.duration_seconds
        if plan is not None:
            middle = (start + end) / 2
            found = next(
                (i for i, r in enumerate(kept) if r.start_seconds <= middle <= r.end_seconds),
                None,
            )
            if found is None:
                dropped += 1
                continue
            span = kept[found]
            mapped_start = output_time(plan, max(start, span.start_seconds))
            mapped_end = output_time(plan, min(end, span.end_seconds))
            mapped_segment_end = output_time(plan, span.end_seconds)
            if mapped_start is None or mapped_end is None or mapped_segment_end is None:
                dropped += 1
                continue
            start, end, segment, segment_end = (
                mapped_start,
                mapped_end,
                found,
                mapped_segment_end,
            )
        words.append(Cue(_ms(start), _ms(end), (text,), segment, _ms(segment_end)))
    return words, dropped


def _join(first: Cue, second: Cue) -> Cue:
    return replace(first, end_ms=max(first.end_ms, second.end_ms), words=first.words + second.words)


def _fits(first: Cue, second: Cue, max_ms: int) -> bool:
    """Same kept segment, no long pause between, and the joined cue within max_ms."""
    return (
        first.segment == second.segment
        and second.start_ms - first.end_ms <= PAUSE_MS
        and second.end_ms - first.start_ms <= max_ms
    )


def chunk(words: list[Cue], words_per_cue: int, max_ms: int) -> list[Cue]:
    """Group words into cues of up to words_per_cue; never across a cut, pause or max_ms."""
    cues: list[Cue] = []
    for word in words:
        if cues and len(cues[-1].words) < words_per_cue and _fits(cues[-1], word, max_ms):
            cues[-1] = _join(cues[-1], word)
        else:
            cues.append(word)
    return cues


def merge_short(cues: list[Cue], min_ms: int, max_ms: int) -> list[Cue]:
    """Merge cues under min_ms into a neighbour when _fits allows it.

    A short cue first joins the cue after it, else the cue before it. One that cannot
    merge is lengthened toward min_ms, stopping at the next cue and its segment end.
    """

    def short(cue: Cue) -> bool:
        return cue.end_ms - cue.start_ms < min_ms

    forward: list[Cue] = []
    for cue in cues:
        if forward and short(forward[-1]) and _fits(forward[-1], cue, max_ms):
            forward[-1] = _join(forward[-1], cue)
        else:
            forward.append(cue)
    merged: list[Cue] = []
    for cue in forward:
        if merged and short(cue) and _fits(merged[-1], cue, max_ms):
            merged[-1] = _join(merged[-1], cue)
        else:
            merged.append(cue)
    result: list[Cue] = []
    for index, cue in enumerate(merged):
        limit = cue.segment_end_ms
        if index + 1 < len(merged):
            limit = min(limit, merged[index + 1].start_ms)
        end = cue.end_ms
        if short(cue):
            end = max(end, min(cue.start_ms + min_ms, limit))
        if limit > cue.start_ms:
            end = min(end, limit)
        result.append(replace(cue, end_ms=max(end, cue.start_ms + 1)))
    return result


def build_cues(
    transcript: Transcript,
    plan: CutPlan | None,
    *,
    words_per_cue: int,
    max_cue_seconds: float,
    min_cue_seconds: float,
    uppercase: bool,
) -> tuple[list[Cue], int]:
    """Final cues in output time and the number of words dropped inside cuts."""
    words, dropped = timed_words(transcript, plan)
    max_ms, min_ms = _ms(max_cue_seconds), _ms(min_cue_seconds)
    cues = merge_short(chunk(words, words_per_cue, max_ms), min_ms, max_ms)
    if uppercase:
        cues = [replace(cue, words=tuple(w.upper() for w in cue.words)) for cue in cues]
    return cues, dropped


def timestamp(ms: int) -> str:
    """SRT time HH:MM:SS,mmm."""
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    """Numbered SRT blocks separated by blank lines, LF line endings."""
    return "".join(
        f"{number}\n{timestamp(cue.start_ms)} --> {timestamp(cue.end_ms)}\n{cue.text}\n\n"
        for number, cue in enumerate(cues, start=1)
    )
