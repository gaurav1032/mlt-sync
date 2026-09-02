"""Animate still images with Shotcut's *Size, Position & Rotate* filter.

Each preset produces a start rectangle and an end rectangle (``x y w h``) in
project pixels; the image is scaled up by ``scale`` so that panning never
reveals the background. The result is a regular keyframed ``affine`` filter
that you can still tweak inside Shotcut.
"""
from __future__ import annotations

import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable, Optional

from .mlt import MltDoc, MltError, Track
from .timecode import Profile

Rect = tuple[float, float, float, float]
PresetFn = Callable[[Profile, float], tuple[Rect, Rect]]

FILTER_TAG = "affineSizePosition"


def _centered(p: Profile, scale: float) -> Rect:
    w, h = p.width * scale, p.height * scale
    return ((p.width - w) / 2, (p.height - h) / 2, w, h)


def _edges(p: Profile, scale: float):
    w, h = p.width * scale, p.height * scale
    return {
        "left": 0.0,
        "right": p.width - w,
        "top": 0.0,
        "bottom": p.height - h,
        "cx": (p.width - w) / 2,
        "cy": (p.height - h) / 2,
        "w": w,
        "h": h,
    }


def pan_left(p: Profile, s: float):
    e = _edges(p, s)
    return (e["left"], e["cy"], e["w"], e["h"]), (e["right"], e["cy"], e["w"], e["h"])


def pan_right(p: Profile, s: float):
    e = _edges(p, s)
    return (e["right"], e["cy"], e["w"], e["h"]), (e["left"], e["cy"], e["w"], e["h"])


def pan_up(p: Profile, s: float):
    e = _edges(p, s)
    return (e["cx"], e["top"], e["w"], e["h"]), (e["cx"], e["bottom"], e["w"], e["h"])


def pan_down(p: Profile, s: float):
    e = _edges(p, s)
    return (e["cx"], e["bottom"], e["w"], e["h"]), (e["cx"], e["top"], e["w"], e["h"])


def zoom_in(p: Profile, s: float):
    return _centered(p, 1.0), _centered(p, s)


def zoom_out(p: Profile, s: float):
    return _centered(p, s), _centered(p, 1.0)


@dataclass(frozen=True)
class Preset:
    name: str
    description: str
    fn: PresetFn


PRESETS: dict[str, Preset] = {
    "pan-left": Preset("pan-left", "Image slides to the left (camera pans right)", pan_left),
    "pan-right": Preset("pan-right", "Image slides to the right (camera pans left)", pan_right),
    "pan-up": Preset("pan-up", "Image slides upward (camera tilts down)", pan_up),
    "pan-down": Preset("pan-down", "Image slides downward (camera tilts up)", pan_down),
    "zoom-in": Preset("zoom-in", "Slow push-in from 100% to --scale", zoom_in),
    "zoom-out": Preset("zoom-out", "Slow pull-out from --scale to 100%", zoom_out),
    "none": Preset("none", "Leave the clip static (useful for per-clip overrides)", lambda p, s: (_centered(p, 1.0), _centered(p, 1.0))),
}

DEFAULT_PRESETS = ["pan-left", "pan-right", "pan-up", "pan-down"]
MODES = ("cycle", "random", "single", "ping-pong")
EASINGS = {"linear": "=", "smooth": "~=", "step": "|="}


def parse_preset_list(text: str) -> list[str]:
    names = [n.strip().lower() for n in text.replace(";", ",").split(",") if n.strip()]
    unknown = [n for n in names if n not in PRESETS]
    if unknown:
        raise MltError(f"unknown preset(s): {', '.join(unknown)}. Run `presets` to list them.")
    if not names:
        raise MltError("preset list is empty")
    return names


def parse_overrides(pairs: Optional[list[str]]) -> dict[int, str]:
    """``["3=pan-up", "5=none"]`` -> ``{3: 'pan-up', 5: 'none'}`` (1-based clip numbers)."""
    out: dict[int, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise MltError(f"bad --clip override {pair!r}; expected N=preset")
        idx, name = pair.split("=", 1)
        name = name.strip().lower()
        if name not in PRESETS:
            raise MltError(f"unknown preset {name!r} in --clip {pair!r}")
        try:
            out[int(idx)] = name
        except ValueError as exc:
            raise MltError(f"bad clip number in --clip {pair!r}") from exc
    return out


def _fmt(v: float) -> str:
    r = round(v, 2)
    return str(int(r)) if r == int(r) else f"{r:.2f}".rstrip("0").rstrip(".")


def rect_str(r: Rect) -> str:
    return " ".join(_fmt(v) for v in r) + " 1"


def build_filter(doc: MltDoc, frames: int, start: Rect, end: Rect, easing: str = "linear") -> ET.Element:
    op = EASINGS.get(easing)
    if op is None:
        raise MltError(f"unknown easing {easing!r} (use {', '.join(EASINGS)})")
    last = max(0, frames - 1)
    keyframes = f"{doc.tc(0)}{op}{rect_str(start)};{doc.tc(last)}{op}{rect_str(end)}"
    f = ET.Element("filter", id=doc.new_id("filter"), out=doc.tc(last))
    for k, v in [
        ("background", "color:#00000000"),
        ("mlt_service", "affine"),
        ("shotcut:filter", FILTER_TAG),
        ("transition.fill", "1"),
        ("transition.distort", "0"),
        ("transition.rect", keyframes),
        ("transition.valign", "middle"),
        ("transition.halign", "center"),
        ("shotcut:animIn", doc.tc(0)),
        ("shotcut:animOut", doc.tc(0)),
        ("transition.threads", "0"),
        ("transition.fix_rotate_x", "0"),
    ]:
        doc.set_prop(f, k, v)
    return f


def has_spr_filter(producer: ET.Element) -> bool:
    return any(MltDoc.prop(f, "shotcut:filter") == FILTER_TAG for f in producer.findall("filter"))


@dataclass
class AnimatedClip:
    index: int
    caption: str
    preset: str
    frames: int
    producer_id: str


@dataclass
class AnimateReport:
    track: str
    clips: list[AnimatedClip] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def choose_presets(count: int, presets: list[str], mode: str, seed: Optional[int]) -> list[str]:
    if mode not in MODES:
        raise MltError(f"unknown mode {mode!r} (use {', '.join(MODES)})")
    if mode == "single":
        return [presets[0]] * count
    if mode == "cycle":
        return [presets[i % len(presets)] for i in range(count)]
    if mode == "ping-pong":
        if len(presets) == 1:
            return [presets[0]] * count
        seq = presets + presets[-2:0:-1]
        return [seq[i % len(seq)] for i in range(count)]
    rng = random.Random(seed)
    out: list[str] = []
    prev = None
    for _ in range(count):
        choices = [p for p in presets if p != prev] or presets
        pick = rng.choice(choices)
        out.append(pick)
        prev = pick
    return out


def animate_track(
    doc: MltDoc,
    track: Track,
    presets: list[str],
    mode: str = "cycle",
    scale: float = 1.15,
    seed: Optional[int] = None,
    overrides: Optional[dict[int, str]] = None,
    replace: bool = False,
    easing: str = "linear",
    images_only: bool = True,
) -> AnimateReport:
    if scale < 1.0:
        raise MltError("--scale must be >= 1.0 (1.15 means the image is 15% larger than the frame)")
    report = AnimateReport(track=track.label)
    clips = list(doc.timeline_clips(track))
    if not clips:
        report.warnings.append(f"track {track.label} has no clips - run `sync` first?")
        return report

    plan = choose_presets(len(clips), presets, mode, seed)
    overrides = overrides or {}
    profile = doc.profile

    for n, (item, producer) in enumerate(clips, start=1):
        caption = doc.caption(producer)
        if images_only and doc.media_kind(producer) != "image":
            report.skipped.append(f"#{n} {caption}: not an image")
            continue
        if has_spr_filter(producer):
            if not replace:
                report.skipped.append(f"#{n} {caption}: already has a Size/Position filter (use --replace)")
                continue
            for f in list(producer.findall("filter")):
                if doc.prop(f, "shotcut:filter") == FILTER_TAG:
                    producer.remove(f)

        # if the same producer backs several timeline entries, give this entry its own copy
        if doc.entry_references(producer.get("id", "")) > 1:
            clone = doc.clone_producer(producer, item.frames)
            doc.insert_before(track.element, clone)
            item.element.set("producer", clone.get("id", ""))
            producer = clone

        name = overrides.get(n, plan[n - 1])
        if name == "none":
            report.skipped.append(f"#{n} {caption}: preset 'none'")
            continue
        start, end = PRESETS[name].fn(profile, scale)
        producer.append(build_filter(doc, item.frames, start, end, easing))
        report.clips.append(AnimatedClip(n, caption, name, item.frames, producer.get("id", "")))
    return report


def clear_track_filters(doc: MltDoc, track: Track) -> int:
    removed = 0
    for _, producer in doc.timeline_clips(track):
        for f in list(producer.findall("filter")):
            if doc.prop(f, "shotcut:filter") == FILTER_TAG:
                producer.remove(f)
                removed += 1
    return removed
