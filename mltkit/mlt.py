"""A thin, dependency-free model over Shotcut's MLT XML.

Shotcut project layout (simplified)::

    <mlt producer="main_bin">
      <profile .../>
      <playlist id="main_bin"> ...clips imported into the Playlist panel... </playlist>
      <producer id="black"/> <playlist id="background"/>
      <producer|chain id="..."/>           one per timeline clip
      <playlist id="playlist0">  V1  </playlist>
      <playlist id="playlist1">  A1  </playlist>
      <tractor id="tractor0"> <track producer="background"/> <track producer="playlist0"/> ... </tractor>
    </mlt>

Everything here is expressed in frames; conversion happens at the edges.
"""
from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Iterator, Optional

from .media import image_size, kind_from_path, relative_resource
from .timecode import Profile, frames_to_timecode, timecode_to_frames

XML_HEADER = '<?xml version="1.0" standalone="no"?>\n'
SHOTCUT_VERSION = "24.11.17"
MLT_VERSION = "7.28.0"


class MltError(Exception):
    """Raised for structural problems in a project file."""


@dataclass
class Track:
    index: int  # position inside the tractor (0 = background)
    element: ET.Element  # the <playlist>
    id: str
    name: str
    kind: str  # video | audio | background | unknown
    hide: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})"


@dataclass
class Item:
    """One child of a playlist: an <entry> or a <blank>."""

    element: ET.Element
    start: int
    frames: int
    producer_id: Optional[str] = None
    in_frame: int = 0
    out_frame: int = 0

    @property
    def is_blank(self) -> bool:
        return self.producer_id is None

    @property
    def end(self) -> int:
        return self.start + self.frames


@dataclass
class MltDoc:
    tree: ET.ElementTree
    path: Optional[Path] = None
    _profile: Optional[Profile] = field(default=None, repr=False)

    # ------------------------------------------------------------------ io
    @classmethod
    def load(cls, path: str) -> "MltDoc":
        p = Path(path)
        if not p.is_file():
            raise MltError(f"project file not found: {path}")
        try:
            tree = ET.parse(p)
        except ET.ParseError as exc:
            raise MltError(f"{path} is not valid XML: {exc}") from exc
        if tree.getroot().tag != "mlt":
            raise MltError(f"{path} is not an MLT project (root element is <{tree.getroot().tag}>)")
        return cls(tree=tree, path=p)

    def save(self, path: str) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        ET.indent(self.root, space="  ")
        body = ET.tostring(self.root, encoding="unicode")
        out.write_text(XML_HEADER + body + "\n", encoding="utf-8")
        return out

    def to_string(self) -> str:
        clone = copy.deepcopy(self.root)
        ET.indent(clone, space="  ")
        return XML_HEADER + ET.tostring(clone, encoding="unicode")

    @property
    def root(self) -> ET.Element:
        return self.tree.getroot()

    @property
    def base_dir(self) -> Optional[str]:
        return str(self.path.parent) if self.path else None

    # ------------------------------------------------------------- profile
    @property
    def profile(self) -> Profile:
        if self._profile is None:
            el = self.root.find("profile")
            if el is None:
                self._profile = Profile()
            else:
                g = lambda k, d: int(el.get(k, d))  # noqa: E731
                self._profile = Profile(
                    width=g("width", 1920),
                    height=g("height", 1080),
                    fps_num=g("frame_rate_num", 30),
                    fps_den=g("frame_rate_den", 1),
                    description=el.get("description", ""),
                    progressive=g("progressive", 1),
                    display_aspect_num=g("display_aspect_num", 16),
                    display_aspect_den=g("display_aspect_den", 9),
                    colorspace=g("colorspace", 709),
                )
        return self._profile

    @property
    def fps(self) -> Fraction:
        return self.profile.fps

    def tc(self, frames: int) -> str:
        return frames_to_timecode(frames, self.fps)

    def frames(self, text: Optional[str], default: int = 0) -> int:
        if text is None or text == "":
            return default
        return timecode_to_frames(text, self.fps)

    # ---------------------------------------------------------- properties
    @staticmethod
    def prop(el: ET.Element, name: str, default: Optional[str] = None) -> Optional[str]:
        for child in el.findall("property"):
            if child.get("name") == name:
                return child.text if child.text is not None else ""
        return default

    @staticmethod
    def set_prop(el: ET.Element, name: str, value: object) -> ET.Element:
        for child in el.findall("property"):
            if child.get("name") == name:
                child.text = str(value)
                return child
        child = ET.SubElement(el, "property", name=name)
        child.text = str(value)
        return child

    @staticmethod
    def del_prop(el: ET.Element, name: str) -> None:
        for child in list(el.findall("property")):
            if child.get("name") == name:
                el.remove(child)

    # ----------------------------------------------------------- lookups
    @property
    def producers(self) -> dict[str, ET.Element]:
        out: dict[str, ET.Element] = {}
        for tag in ("producer", "chain", "tractor", "playlist"):
            for el in self.root.findall(tag):
                pid = el.get("id")
                if pid:
                    out[pid] = el
        return out

    @property
    def playlists(self) -> dict[str, ET.Element]:
        return {el.get("id", ""): el for el in self.root.findall("playlist")}

    @property
    def main_bin(self) -> Optional[ET.Element]:
        return self.playlists.get("main_bin")

    @property
    def tractor(self) -> ET.Element:
        tractors = self.root.findall("tractor")
        if not tractors:
            raise MltError("no <tractor> found - is this really a Shotcut project?")
        for t in tractors:
            if self.prop(t, "shotcut") == "1":
                return t
        return tractors[-1]

    @property
    def tracks(self) -> list[Track]:
        playlists = self.playlists
        tracks: list[Track] = []
        for idx, tr in enumerate(self.tractor.findall("track")):
            pid = tr.get("producer", "")
            pl = playlists.get(pid)
            if pl is None:
                continue
            if pid == "background":
                kind = "background"
            elif self.prop(pl, "shotcut:video") == "1":
                kind = "video"
            elif self.prop(pl, "shotcut:audio") == "1":
                kind = "audio"
            else:
                kind = "audio" if tr.get("hide") == "video" else "unknown"
            name = self.prop(pl, "shotcut:name") or pid
            tracks.append(Track(idx, pl, pid, name, kind, tr.get("hide", "")))
        return tracks

    @property
    def video_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "video"]

    @property
    def audio_tracks(self) -> list[Track]:
        return [t for t in self.tracks if t.kind == "audio"]

    def find_track(self, spec: Optional[str], kind: str) -> Track:
        """Resolve ``V1`` / ``playlist0`` / ``My Track`` / ``2`` to a Track."""
        candidates = [t for t in self.tracks if t.kind == kind]
        if not candidates:
            raise MltError(f"the project has no {kind} track")
        if not spec:
            return candidates[0]
        s = spec.strip().lower()
        for t in candidates:
            if s in (t.name.lower(), t.id.lower()):
                return t
        if s.isdigit() and 0 < int(s) <= len(candidates):
            return candidates[int(s) - 1]
        names = ", ".join(t.name for t in candidates)
        raise MltError(f"{kind} track {spec!r} not found (available: {names})")

    # ------------------------------------------------------------- media
    def media_kind(self, producer: ET.Element) -> str:
        service = self.prop(producer, "mlt_service", "") or ""
        if service in ("qimage", "pixbuf"):
            return "image"
        if service == "color" or service == "colour":
            return "color"
        if service in ("dynamictext", "qtext", "glaxnimate", "timewarp"):
            return "other"
        resource = self.prop(producer, "resource", "") or ""
        if service.startswith("avformat"):
            if self.prop(producer, "video_index") == "-1":
                return "audio"
            k = kind_from_path(resource)
            return k if k in ("audio", "video") else "video"
        return kind_from_path(resource)

    def resource(self, producer: ET.Element) -> str:
        return self.prop(producer, "resource", "") or ""

    def caption(self, producer: ET.Element) -> str:
        return self.prop(producer, "shotcut:caption") or Path(self.resource(producer)).name

    def bin_producers(self) -> list[ET.Element]:
        bin_pl = self.main_bin
        if bin_pl is None:
            return []
        producers = self.producers
        out: list[ET.Element] = []
        for entry in bin_pl.findall("entry"):
            el = producers.get(entry.get("producer", ""))
            if el is not None:
                out.append(el)
        return out

    def producer_frames(self, producer: ET.Element) -> int:
        length = self.prop(producer, "length")
        if length:
            try:
                return self.frames(length)
            except ValueError:
                pass
        out = producer.get("out")
        return self.frames(out) + 1 if out else 0

    # ---------------------------------------------------------- playlists
    def items(self, playlist: ET.Element) -> list[Item]:
        cursor = 0
        out: list[Item] = []
        for child in playlist:
            if child.tag == "blank":
                n = self.frames(child.get("length"))
                out.append(Item(child, cursor, n))
                cursor += n
            elif child.tag == "entry":
                i = self.frames(child.get("in"))
                o = self.frames(child.get("out"))
                n = max(0, o - i + 1)
                out.append(Item(child, cursor, n, child.get("producer"), i, o))
                cursor += n
        return out

    def playlist_frames(self, playlist: ET.Element) -> int:
        items = self.items(playlist)
        return items[-1].end if items else 0

    def clear_playlist(self, playlist: ET.Element) -> int:
        removed = 0
        for child in list(playlist):
            if child.tag in ("blank", "entry"):
                playlist.remove(child)
                removed += 1
        return removed

    def add_blank(self, playlist: ET.Element, frames: int) -> None:
        if frames <= 0:
            return
        ET.SubElement(playlist, "blank", length=self.tc(frames))

    def add_entry(self, playlist: ET.Element, producer_id: str, in_frame: int, out_frame: int) -> ET.Element:
        return ET.SubElement(
            playlist,
            "entry",
            producer=producer_id,
            **{"in": self.tc(in_frame), "out": self.tc(out_frame)},
        )

    def entry_references(self, producer_id: str) -> int:
        """How many timeline entries (excluding main_bin) reference a producer."""
        count = 0
        for pl in self.root.findall("playlist"):
            if pl.get("id") == "main_bin":
                continue
            count += sum(1 for e in pl.findall("entry") if e.get("producer") == producer_id)
        return count

    # ---------------------------------------------------------- producers
    def new_id(self, prefix: str) -> str:
        existing = {el.get("id") for el in self.root.iter() if el.get("id")}
        n = 0
        while f"{prefix}{n}" in existing:
            n += 1
        return f"{prefix}{n}"

    def insert_before(self, anchor: ET.Element, new: ET.Element) -> None:
        children = list(self.root)
        idx = children.index(anchor)
        self.root.insert(idx, new)

    def clone_producer(self, src: ET.Element, frames: int, keep_filters: bool = True) -> ET.Element:
        """Deep-copy a bin producer for use as a timeline clip of ``frames`` length."""
        clone = copy.deepcopy(src)
        clone.set("id", self.new_id(src.tag))
        clone.set("in", self.tc(0))
        clone.set("out", self.tc(frames - 1))
        if not keep_filters:
            for f in list(clone.findall("filter")):
                clone.remove(f)
        if self.media_kind(src) in ("image", "color"):
            self.set_prop(clone, "length", self.tc(frames))
        # a clip that came from the bin should not drag its hash-cache with it
        self.del_prop(clone, "shotcut:hash")
        return clone

    def new_image_producer(self, image_path: str, frames: int, absolute: bool = False) -> ET.Element:
        el = ET.Element("producer", id=self.new_id("producer"))
        el.set("in", self.tc(0))
        el.set("out", self.tc(frames - 1))
        props = [
            ("length", self.tc(frames)),
            ("eof", "pause"),
            ("resource", relative_resource(image_path, self.base_dir, absolute)),
            ("ttl", "1"),
            ("aspect_ratio", "1"),
            ("meta.media.progressive", "1"),
            ("seekable", "1"),
            ("format", "1"),
            ("mlt_service", "qimage"),
            ("shotcut:caption", Path(image_path).name),
            ("shotcut:skipConvert", "1"),
            ("xml", "was here"),
        ]
        size = image_size(image_path)
        if size:
            props.insert(8, ("meta.media.width", str(size[0])))
            props.insert(9, ("meta.media.height", str(size[1])))
        for k, v in props:
            self.set_prop(el, k, v)
        return el

    def new_audio_chain(self, audio_path: str, frames: int, absolute: bool = False) -> ET.Element:
        el = ET.Element("chain", id=self.new_id("chain"))
        el.set("out", self.tc(frames - 1))
        for k, v in [
            ("length", self.tc(frames)),
            ("eof", "pause"),
            ("resource", relative_resource(audio_path, self.base_dir, absolute)),
            ("mlt_service", "avformat-novalidate"),
            ("seekable", "1"),
            ("audio_index", "0"),
            ("video_index", "-1"),
            ("mute_on_pause", "0"),
            ("shotcut:caption", Path(audio_path).name),
            ("xml", "was here"),
        ]:
            self.set_prop(el, k, v)
        return el

    # ----------------------------------------------------------- finalize
    def timeline_frames(self) -> int:
        return max((self.playlist_frames(t.element) for t in self.tracks if t.kind != "background"), default=0)

    def finalize(self) -> int:
        """Make background / black / tractor lengths consistent with the tracks."""
        total = max(1, self.timeline_frames())
        producers = self.producers
        black = producers.get("black")
        if black is not None:
            black.set("in", self.tc(0))
            black.set("out", self.tc(total - 1))
            self.set_prop(black, "length", self.tc(total))
        bg = self.playlists.get("background")
        if bg is not None:
            self.clear_playlist(bg)
            self.add_entry(bg, "black", 0, total - 1)
        tractor = self.tractor
        tractor.set("in", self.tc(0))
        tractor.set("out", self.tc(total - 1))
        return total

    # ------------------------------------------------------------ iterate
    def timeline_clips(self, track: Track) -> Iterator[tuple[Item, ET.Element]]:
        producers = self.producers
        for item in self.items(track.element):
            if item.is_blank:
                continue
            producer = producers.get(item.producer_id or "")
            if producer is not None:
                yield item, producer
