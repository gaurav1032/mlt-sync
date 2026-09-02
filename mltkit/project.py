"""Create a brand-new Shotcut project (``starting.mlt``) without opening Shotcut."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .media import list_images, wav_duration
from .mlt import MLT_VERSION, SHOTCUT_VERSION, MltDoc, MltError
from .timecode import Profile, seconds_to_frames

PROFILES: dict[str, Profile] = {
    "1080p30": Profile(1920, 1080, 30, 1, "HD 1080p 30 fps"),
    "1080p60": Profile(1920, 1080, 60, 1, "HD 1080p 60 fps"),
    "1080p25": Profile(1920, 1080, 25, 1, "HD 1080p 25 fps"),
    "1080p24": Profile(1920, 1080, 24, 1, "HD 1080p 24 fps"),
    "720p30": Profile(1280, 720, 30, 1, "HD 720p 30 fps"),
    "4k30": Profile(3840, 2160, 30, 1, "UHD 2160p 30 fps"),
    "vertical1080p30": Profile(1080, 1920, 30, 1, "Vertical HD 30 fps", display_aspect_num=9, display_aspect_den=16),
    "square1080p30": Profile(1080, 1080, 30, 1, "Square 1080p 30 fps", display_aspect_num=1, display_aspect_den=1),
}


def _prop(el: ET.Element, name: str, value: object) -> None:
    ET.SubElement(el, "property", name=name).text = str(value)


def new_project(
    out_path: str,
    profile: Profile,
    images: list[Path],
    audio: Optional[str] = None,
    audio_seconds: Optional[float] = None,
    image_seconds: float = 4.0,
    absolute_paths: bool = False,
) -> MltDoc:
    fps = profile.fps
    root = ET.Element(
        "mlt",
        LC_NUMERIC="C",
        version=MLT_VERSION,
        title=f"Shotcut version {SHOTCUT_VERSION}",
        producer="main_bin",
    )
    ET.SubElement(
        root,
        "profile",
        description=profile.description,
        width=str(profile.width),
        height=str(profile.height),
        progressive=str(profile.progressive),
        sample_aspect_num="1",
        sample_aspect_den="1",
        display_aspect_num=str(profile.display_aspect_num),
        display_aspect_den=str(profile.display_aspect_den),
        frame_rate_num=str(profile.fps_num),
        frame_rate_den=str(profile.fps_den),
        colorspace=str(profile.colorspace),
    )
    main_bin = ET.SubElement(root, "playlist", id="main_bin")
    _prop(main_bin, "xml_retain", "1")

    black = ET.SubElement(root, "producer", id="black")
    _prop(black, "length", "00:00:01.000")
    _prop(black, "eof", "pause")
    _prop(black, "resource", "0")
    _prop(black, "aspect_ratio", "1")
    _prop(black, "mlt_service", "color")
    _prop(black, "mlt_image_format", "rgba")
    _prop(black, "set.test_audio", "0")
    ET.SubElement(root, "playlist", id="background")

    v1 = ET.SubElement(root, "playlist", id="playlist0")
    _prop(v1, "shotcut:video", "1")
    _prop(v1, "shotcut:name", "V1")
    a1 = ET.SubElement(root, "playlist", id="playlist1")
    _prop(a1, "shotcut:audio", "1")
    _prop(a1, "shotcut:name", "A1")

    tractor = ET.SubElement(root, "tractor", id="tractor0", title=f"Shotcut version {SHOTCUT_VERSION}")
    _prop(tractor, "shotcut", "1")
    _prop(tractor, "shotcut:projectAudioChannels", "2")
    _prop(tractor, "shotcut:projectFolder", "0")
    ET.SubElement(tractor, "track", producer="background")
    ET.SubElement(tractor, "track", producer="playlist0")
    ET.SubElement(tractor, "track", producer="playlist1", hide="video")

    mix = ET.SubElement(tractor, "transition", id="transition0")
    _prop(mix, "a_track", "0")
    _prop(mix, "b_track", "1")
    _prop(mix, "mlt_service", "mix")
    _prop(mix, "always_active", "1")
    _prop(mix, "sum", "1")
    blend = ET.SubElement(tractor, "transition", id="transition1")
    _prop(blend, "a_track", "0")
    _prop(blend, "b_track", "1")
    _prop(blend, "version", "0.1")
    _prop(blend, "mlt_service", "frei0r.cairoblend")
    _prop(blend, "threads", "0")
    _prop(blend, "disable", "1")
    mix2 = ET.SubElement(tractor, "transition", id="transition2")
    _prop(mix2, "a_track", "0")
    _prop(mix2, "b_track", "2")
    _prop(mix2, "mlt_service", "mix")
    _prop(mix2, "always_active", "1")
    _prop(mix2, "sum", "1")

    doc = MltDoc(ET.ElementTree(root), Path(out_path))

    # --- bin content -------------------------------------------------------
    img_frames = max(1, seconds_to_frames(image_seconds, fps))
    for img in images:
        producer = doc.new_image_producer(str(img), img_frames, absolute=absolute_paths)
        doc.insert_before(black, producer)
        doc.add_entry(main_bin, producer.get("id", ""), 0, img_frames - 1)

    if audio:
        if not Path(audio).is_file():
            raise MltError(f"audio file not found: {audio}")
        seconds = audio_seconds if audio_seconds is not None else wav_duration(audio)
        if seconds is None:
            raise MltError(
                "cannot determine the audio duration (only .wav can be probed without ffmpeg). "
                "Pass --duration SECONDS or --beats beat.json so the last beat's end_time is used."
            )
        frames = max(1, seconds_to_frames(seconds, fps))
        chain = doc.new_audio_chain(audio, frames, absolute=absolute_paths)
        doc.insert_before(black, chain)
        doc.add_entry(main_bin, chain.get("id", ""), 0, frames - 1)

    doc.finalize()
    return doc


def resolve_profile(spec: Optional[str]) -> Profile:
    if not spec:
        return PROFILES["1080p30"]
    key = spec.lower().replace(" ", "").replace("-", "").replace("_", "")
    if key in PROFILES:
        return PROFILES[key]
    # WxH@fps
    try:
        size, fps = key.split("@")
        w, h = size.split("x")
        fps_f = float(fps)
        if fps_f == int(fps_f):
            num, den = int(fps_f), 1
        else:  # 29.97 / 23.976
            num, den = (30000, 1001) if abs(fps_f - 29.97) < 0.01 else (24000, 1001) if abs(fps_f - 23.976) < 0.01 else (60000, 1001)
        return Profile(int(w), int(h), num, den, f"{w}x{h} {fps} fps")
    except ValueError:
        pass
    raise MltError(f"unknown profile {spec!r}. Use one of {', '.join(PROFILES)} or WIDTHxHEIGHT@FPS")


def images_for_new(images_dir: str, pattern: Optional[str]) -> list[Path]:
    files = list_images(images_dir, pattern)
    if not files:
        raise MltError(f"no images found in {images_dir}")
    return files
