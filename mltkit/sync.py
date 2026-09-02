"""Beat sync: lay images onto a video track so each one starts on a beat."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Optional

from .beats import Slot
from .media import list_images, natural_key
from .mlt import MltDoc, MltError, Track


@dataclass
class PlacedClip:
    index: int
    caption: str
    start: int
    frames: int
    text: str
    producer_id: str


@dataclass
class SyncReport:
    track: str
    clips: list[PlacedClip] = field(default_factory=list)
    blanks: int = 0
    warnings: list[str] = field(default_factory=list)
    audio_placed: Optional[str] = None
    total_frames: int = 0


def collect_images(
    doc: MltDoc,
    images_dir: Optional[str] = None,
    pattern: Optional[str] = None,
    bin_order: bool = False,
) -> list[tuple[str, Optional[ET.Element], Optional[str]]]:
    """Return ``[(caption, bin_producer_or_None, path_or_None), ...]`` in playback order."""
    if images_dir:
        files = list_images(images_dir, pattern)
        if not files:
            raise MltError(f"no images found in {images_dir}" + (f" matching {pattern}" if pattern else ""))
        return [(f.name, None, str(f)) for f in files]

    producers = [p for p in doc.bin_producers() if doc.media_kind(p) == "image"]
    if not producers:
        raise MltError(
            "the Playlist (main_bin) has no images. Import them in Shotcut first, or pass --images DIR"
        )
    if not bin_order:
        producers.sort(key=lambda p: natural_key(doc.caption(p)))
    return [(doc.caption(p), p, None) for p in producers]


def sync_images(
    doc: MltDoc,
    slots: list[Slot],
    images: list[tuple[str, Optional[ET.Element], Optional[str]]],
    track: Track,
    overflow: str = "cycle",
    keep_filters: bool = True,
    absolute_paths: bool = False,
) -> SyncReport:
    """Rebuild ``track`` from scratch using ``slots`` and ``images``.

    overflow controls what happens when there are more beats than images:
      cycle  wrap around to the first image again
      hold   keep showing the last image
      stop   leave the remaining beats empty
    """
    if overflow not in ("cycle", "hold", "stop"):
        raise MltError(f"unknown overflow policy {overflow!r}")

    report = SyncReport(track=track.label)
    playlist = track.element
    doc.clear_playlist(playlist)

    n_images = len(images)
    if len(slots) > n_images:
        report.warnings.append(
            f"{len(slots)} beats but only {n_images} images - overflow policy '{overflow}' applied"
        )
    elif n_images > len(slots):
        report.warnings.append(f"{n_images} images but only {len(slots)} beats - last {n_images - len(slots)} image(s) unused")

    cursor = 0
    for i, slot in enumerate(slots):
        if i >= n_images:
            if overflow == "stop":
                break
            img_idx = i % n_images if overflow == "cycle" else n_images - 1
        else:
            img_idx = i

        caption, bin_producer, path = images[img_idx]

        if slot.start > cursor:
            doc.add_blank(playlist, slot.start - cursor)
            report.blanks += 1
            cursor = slot.start
        elif slot.start < cursor:
            report.warnings.append(
                f"beat #{i + 1} starts before the previous clip ended - trimmed by {cursor - slot.start} frame(s)"
            )
        frames = slot.end - cursor
        if frames <= 0:
            report.warnings.append(f"beat #{i + 1} skipped (zero length after trimming)")
            continue

        if bin_producer is not None:
            clip = doc.clone_producer(bin_producer, frames, keep_filters=keep_filters)
        else:
            clip = doc.new_image_producer(path or caption, frames, absolute=absolute_paths)
        doc.insert_before(playlist, clip)
        doc.add_entry(playlist, clip.get("id", ""), 0, frames - 1)

        report.clips.append(PlacedClip(i + 1, caption, cursor, frames, slot.beat.text, clip.get("id", "")))
        cursor += frames

    report.total_frames = cursor
    return report


def place_audio(doc: MltDoc, track: Track, replace: bool = True) -> Optional[str]:
    """Put the first audio clip from the bin at the start of ``track`` if it is empty."""
    playlist = track.element
    if doc.playlist_frames(playlist) > 0 and not replace:
        return None
    audio = [p for p in doc.bin_producers() if doc.media_kind(p) == "audio"]
    if not audio:
        return None
    src = audio[0]
    frames = doc.producer_frames(src)
    if frames <= 0:
        return None
    doc.clear_playlist(playlist)
    clip = doc.clone_producer(src, frames)
    doc.insert_before(playlist, clip)
    doc.add_entry(playlist, clip.get("id", ""), 0, frames - 1)
    return doc.caption(src)
