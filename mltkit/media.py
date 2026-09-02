"""Small stdlib-only media helpers: file kind detection, natural sort,
image dimensions and WAV duration."""
from __future__ import annotations

import os
import re
import struct
import wave
from pathlib import Path
from typing import Iterable, Optional, Tuple

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".svg"}
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".wmv"}


def kind_from_path(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    return "other"


_NAT_RE = re.compile(r"(\d+)")


def natural_key(text: str):
    """Sort ``01.png, 2.png, 10.png`` the way humans expect."""
    base = os.path.basename(str(text))
    return [int(tok) if tok.isdigit() else tok.lower() for tok in _NAT_RE.split(base)]


def list_images(directory: str, pattern: Optional[str] = None) -> list[Path]:
    folder = Path(directory)
    if not folder.is_dir():
        raise FileNotFoundError(f"images directory not found: {directory}")
    if pattern:
        files = sorted(folder.glob(pattern), key=lambda p: natural_key(p.name))
    else:
        files = sorted(
            (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
            key=lambda p: natural_key(p.name),
        )
    return files


def image_size(path: str) -> Optional[Tuple[int, int]]:
    """Return (width, height) for PNG / JPEG / GIF / BMP / WebP without Pillow."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", head[16:24])
                return int(w), int(h)
            if head[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", head[6:10])
                return int(w), int(h)
            if head[:2] == b"BM":
                w, h = struct.unpack("<ii", head[18:26])
                return int(w), abs(int(h))
            if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
                chunk = head[12:16]
                if chunk == b"VP8 ":
                    fh.seek(26)
                    w, h = struct.unpack("<HH", fh.read(4))
                    return w & 0x3FFF, h & 0x3FFF
                if chunk == b"VP8L":
                    fh.seek(21)
                    b = fh.read(4)
                    w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                    h = 1 + (((b[3] & 0xF) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                    return w, h
                if chunk == b"VP8X":
                    fh.seek(24)
                    b = fh.read(6)
                    w = 1 + (b[0] | (b[1] << 8) | (b[2] << 16))
                    h = 1 + (b[3] | (b[4] << 8) | (b[5] << 16))
                    return w, h
            if head[:2] == b"\xff\xd8":
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    if code in (0xD8, 0x01) or 0xD0 <= code <= 0xD7:
                        continue
                    (seg_len,) = struct.unpack(">H", fh.read(2))
                    if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                        fh.read(1)
                        h, w = struct.unpack(">HH", fh.read(4))
                        return int(w), int(h)
                    fh.seek(seg_len - 2, os.SEEK_CUR)
    except (OSError, struct.error):
        return None
    return None


def wav_duration(path: str) -> Optional[float]:
    try:
        with wave.open(path, "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / float(rate) if rate else None
    except (wave.Error, OSError, EOFError):
        return None


def relative_resource(target: str, base_dir: Optional[str], absolute: bool = False) -> str:
    """Path string to store in the .mlt: relative to the project file when possible."""
    target_path = Path(target)
    if absolute or base_dir is None:
        return str(target_path.resolve())
    try:
        return os.path.relpath(target_path.resolve(), Path(base_dir).resolve())
    except ValueError:  # different drive on Windows
        return str(target_path.resolve())


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
