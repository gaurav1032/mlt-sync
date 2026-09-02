"""Optional ``mltkit.json`` config so you don't have to repeat flags.

Precedence: command-line flag > mltkit.json > built-in default.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

CONFIG_NAME = "mltkit.json"

DEFAULTS: dict[str, Any] = {
    # files
    "input": "starting.mlt",
    "beats": "beat.json",
    "sync_output": "after_beat_sync.mlt",
    "anim_output": "after_animation.mlt",
    "images": None,  # directory; None = use the Shotcut Playlist (main_bin)
    "pattern": None,  # glob inside images dir, e.g. "*.png"
    "audio": None,
    # tracks
    "video_track": None,  # None = first video track (V1)
    "audio_track": None,
    # sync
    "beat_mode": "next-beat",
    "offset": 0.0,
    "min_duration": 1,  # frames
    "tail": None,  # seconds for the last image
    "lead_in": True,
    "overflow": "cycle",
    "place_audio": True,
    "bin_order": False,
    # animate
    "presets": "pan-left,pan-right,pan-up,pan-down",
    "mode": "cycle",
    "scale": 1.15,
    "seed": None,
    "easing": "linear",
    "replace": False,
    # misc
    "absolute_paths": False,
    "profile": "1080p30",
    "image_duration": 4.0,
}


def load_config(path: Optional[str]) -> tuple[dict[str, Any], Optional[Path]]:
    candidate = Path(path) if path else Path.cwd() / CONFIG_NAME
    if not candidate.is_file():
        if path:
            raise FileNotFoundError(f"config file not found: {path}")
        return {}, None
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{candidate} must contain a JSON object")
    unknown = sorted(set(data) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"{candidate}: unknown key(s): {', '.join(unknown)}")
    return data, candidate


def write_default_config(path: str, force: bool = False) -> Path:
    target = Path(path)
    if target.exists() and not force:
        raise FileExistsError(f"{target} already exists (use --force to overwrite)")
    target.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    return target


class Settings:
    """Merge argparse values with config + defaults.  ``settings.scale`` etc."""

    def __init__(self, args: Any, config: dict[str, Any]):
        self._args = args
        self._config = config

    def __getattr__(self, key: str) -> Any:
        value = getattr(self._args, key, None)
        if value is not None:
            return value
        if key in self._config and self._config[key] is not None:
            return self._config[key]
        if key in DEFAULTS:
            return DEFAULTS[key]
        raise AttributeError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            return default
