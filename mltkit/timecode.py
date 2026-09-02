"""Frame <-> timecode helpers for MLT / Shotcut.

Shotcut writes times as ``HH:MM:SS.mmm``. MLT also accepts plain frame
counts and ``HH:MM:SS:FF``. Everything here works in *frames* internally so
that rounding is done exactly once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Union

Number = Union[int, float, str, Fraction]


@dataclass(frozen=True)
class Profile:
    width: int = 1920
    height: int = 1080
    fps_num: int = 30
    fps_den: int = 1
    description: str = "HD 1080p 30 fps"
    progressive: int = 1
    display_aspect_num: int = 16
    display_aspect_den: int = 9
    colorspace: int = 709

    @property
    def fps(self) -> Fraction:
        return Fraction(self.fps_num, self.fps_den)

    @property
    def fps_label(self) -> str:
        f = self.fps
        return str(f.numerator) if f.denominator == 1 else f"{float(f):.3f}"


def to_fraction(value: Number) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(repr(value))
    return Fraction(str(value).strip())


def seconds_to_frames(seconds: Number, fps: Fraction) -> int:
    return int(round(to_fraction(seconds) * fps))


def frames_to_seconds(frames: int, fps: Fraction) -> float:
    return float(Fraction(frames) / fps)


def frames_to_timecode(frames: int, fps: Fraction) -> str:
    """Format a frame count the way Shotcut does: ``HH:MM:SS.mmm``."""
    frames = max(0, int(frames))
    total_ms = int(round(Fraction(frames) * 1000 / fps))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


_INT_RE = re.compile(r"^-?\d+$")


def timecode_to_frames(text: str, fps: Fraction) -> int:
    """Parse ``HH:MM:SS.mmm``, ``HH:MM:SS:FF``, ``SS.sss`` or a bare frame count."""
    text = str(text).strip()
    if not text:
        raise ValueError("empty timecode")
    if _INT_RE.match(text):
        return int(text)
    parts = text.split(":")
    if len(parts) > 4:
        raise ValueError(f"invalid timecode: {text!r}")
    frames_part = 0
    if len(parts) == 4:  # HH:MM:SS:FF
        frames_part = int(parts.pop())
    secs = Fraction(parts[-1])
    if len(parts) >= 2:
        secs += int(parts[-2]) * 60
    if len(parts) == 3:
        secs += int(parts[0]) * 3600
    return int(round(secs * fps)) + frames_part


def pretty_seconds(seconds: float) -> str:
    """Human readable ``m:ss.mmm`` used in CLI tables."""
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes)}:{secs:06.3f}"
