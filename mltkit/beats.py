"""Load ``beat.json`` and turn it into frame-accurate slots on the timeline."""
from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Optional

from .timecode import seconds_to_frames


class BeatError(Exception):
    pass


@dataclass(frozen=True)
class Beat:
    index: int
    start: Fraction  # seconds
    end: Fraction  # seconds
    text: str = ""


@dataclass(frozen=True)
class Slot:
    """Where a beat lands on the timeline, in frames."""

    beat: Beat
    start: int
    frames: int

    @property
    def end(self) -> int:
        return self.start + self.frames


_START_KEYS = ("start_time", "start", "from", "begin", "t0")
_END_KEYS = ("end_time", "end", "to", "finish", "t1")
_TEXT_KEYS = ("beat", "text", "caption", "line", "word", "label")


def _pick(d: dict, keys: Iterable[str], required: bool = True):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    if required:
        raise BeatError(f"beat entry is missing one of {list(keys)}: {d}")
    return None


def _seconds(value) -> Fraction:
    if isinstance(value, str) and ":" in value:  # allow mm:ss.mmm / hh:mm:ss.mmm
        parts = value.split(":")
        total = Fraction(0)
        for part in parts:
            total = total * 60 + Fraction(part)
        return total
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise BeatError(f"cannot parse time value {value!r}") from exc


def load_beats(path: str) -> list[Beat]:
    p = Path(path)
    if not p.is_file():
        raise BeatError(f"beat file not found: {path}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BeatError(f"{path} is not valid JSON: {exc}") from exc

    if isinstance(data, dict):
        for key in ("beats", "segments", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            raise BeatError("beat.json must be a list, or an object with a 'beats' list")
    if not isinstance(data, list) or not data:
        raise BeatError("beat.json contains no beats")

    beats: list[Beat] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise BeatError(f"beat #{i + 1} is not an object: {raw!r}")
        start = _seconds(_pick(raw, _START_KEYS))
        end_raw = _pick(raw, _END_KEYS, required=False)
        end = _seconds(end_raw) if end_raw is not None else start
        text = str(_pick(raw, _TEXT_KEYS, required=False) or "")
        if end < start:
            raise BeatError(f"beat #{i + 1} ends ({end}) before it starts ({start})")
        beats.append(Beat(i, start, end, text))

    beats.sort(key=lambda b: b.start)
    return beats


def build_slots(
    beats: list[Beat],
    fps: Fraction,
    mode: str = "next-beat",
    offset: float = 0.0,
    min_frames: int = 1,
    tail: Optional[float] = None,
    lead_in: bool = True,
) -> list[Slot]:
    """Compute a timeline slot per beat.

    mode:
      next-beat  image N is shown from start[N] until start[N+1] (no gaps).
                 The last image uses its own end_time (or ``tail`` seconds).
      literal    image N is shown from start[N] to end[N]; gaps become blanks.
    offset       shift every beat by N seconds (negative allowed).
    min_frames   never make a clip shorter than this many frames.
    tail         override the duration (seconds) of the very last image.
    lead_in      when True, the first image also covers the silence before the first beat.
    """
    if mode not in ("next-beat", "literal"):
        raise BeatError(f"unknown beat mode {mode!r} (use next-beat or literal)")

    off = Fraction(repr(float(offset)))
    starts = [max(0, seconds_to_frames(b.start + off, fps)) for b in beats]
    ends = [max(0, seconds_to_frames(b.end + off, fps)) for b in beats]

    slots: list[Slot] = []
    for i, beat in enumerate(beats):
        start = starts[i]
        if i == 0 and lead_in:
            start = 0
        is_last = i == len(beats) - 1
        if mode == "next-beat" and not is_last:
            end = starts[i + 1]
        else:
            end = ends[i]
            if is_last and tail is not None:
                end = start + seconds_to_frames(tail, fps)
        frames = max(min_frames, end - start)
        if not is_last:
            # never spill into the next beat's slot
            frames = min(frames, max(1, starts[i + 1] - start))
        slots.append(Slot(beat, start, frames))
    return slots
