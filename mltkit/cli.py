"""mltkit - beat-sync and animate Shotcut (.mlt) projects from the terminal."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .animate import (
    DEFAULT_PRESETS,
    EASINGS,
    MODES,
    PRESETS,
    animate_track,
    clear_track_filters,
    parse_overrides,
    parse_preset_list,
)
from .beats import BeatError, build_slots, load_beats
from .config import CONFIG_NAME, Settings, load_config, write_default_config
from .mlt import MltDoc, MltError
from .project import PROFILES, images_for_new, new_project, resolve_profile
from .sync import collect_images, place_audio, sync_images
from .timecode import frames_to_seconds, pretty_seconds

# ----------------------------------------------------------------- output


class UI:
    def __init__(self, color: bool, quiet: bool = False):
        self.color = color
        self.quiet = quiet

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def bold(self, t: str) -> str:
        return self._c("1", t)

    def dim(self, t: str) -> str:
        return self._c("2", t)

    def green(self, t: str) -> str:
        return self._c("32", t)

    def yellow(self, t: str) -> str:
        return self._c("33", t)

    def red(self, t: str) -> str:
        return self._c("31", t)

    def cyan(self, t: str) -> str:
        return self._c("36", t)

    def say(self, *parts: str) -> None:
        if not self.quiet:
            print(" ".join(parts))

    def ok(self, text: str) -> None:
        self.say(self.green("done"), text)

    def warn(self, text: str) -> None:
        print(self.yellow("warn"), text, file=sys.stderr)

    def error(self, text: str) -> None:
        print(self.red("error"), text, file=sys.stderr)

    def heading(self, text: str) -> None:
        self.say(self.bold(text))

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[str]], max_width: int = 48) -> None:
        if self.quiet:
            return
        rows = [[(c if len(c) <= max_width else c[: max_width - 1] + "…") for c in map(str, r)] for r in rows]
        widths = [len(h) for h in headers]
        for r in rows:
            for i, c in enumerate(r):
                widths[i] = max(widths[i], len(c))
        line = "  ".join(self.dim(h.ljust(widths[i])) for i, h in enumerate(headers))
        print("  " + line)
        for r in rows:
            print("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(r)))


def _use_color(flag: Optional[bool]) -> bool:
    if flag is not None:
        return flag
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


# ------------------------------------------------------------ arg helpers


def _add_sync_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("sync options")
    g.add_argument("--images", metavar="DIR", help="use images from DIR instead of the Shotcut Playlist")
    g.add_argument("--pattern", metavar="GLOB", help="glob inside --images, e.g. '*.png'")
    g.add_argument("--video-track", metavar="NAME", help="video track to fill (default: first, usually V1)")
    g.add_argument("--beat-mode", choices=["next-beat", "literal"], help="hold until next beat (default) or use end_time literally")
    g.add_argument("--offset", type=float, metavar="SEC", help="shift every beat by SEC seconds")
    g.add_argument("--min-duration", type=int, metavar="FRAMES", help="minimum clip length in frames")
    g.add_argument("--tail", type=float, metavar="SEC", help="duration of the last image in seconds")
    g.add_argument("--no-lead-in", dest="lead_in", action="store_false", default=None, help="leave a gap before the first beat instead of starting image 1 at 0")
    g.add_argument("--overflow", choices=["cycle", "hold", "stop"], help="when beats outnumber images")
    g.add_argument("--bin-order", action="store_true", default=None, help="keep Playlist order instead of natural filename sort")
    g.add_argument("--no-audio", dest="place_audio", action="store_false", default=None, help="do not place the bin's audio on the audio track")
    g.add_argument("--audio-track", metavar="NAME", help="audio track to fill (default: first, usually A1)")
    g.add_argument("--strip-filters", action="store_true", default=False, help="drop filters that were on the Playlist clips")
    g.add_argument("--absolute-paths", action="store_true", default=None, help="store absolute file paths in the .mlt")


def _add_anim_opts(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("animate options")
    g.add_argument("--presets", metavar="LIST", help="comma list, e.g. pan-left,pan-right (default: all four pans)")
    g.add_argument("--mode", choices=list(MODES), help="how presets are assigned across clips")
    g.add_argument("--scale", type=float, metavar="FACTOR", help="zoom factor for panning, 1.15 = 15%% larger")
    g.add_argument("--seed", type=int, help="seed for --mode random")
    g.add_argument("--easing", choices=list(EASINGS), help="keyframe interpolation")
    g.add_argument("--clip", action="append", metavar="N=PRESET", help="override clip N (1-based); repeatable")
    g.add_argument("--replace", action="store_true", default=None, help="replace existing Size/Position filters")
    g.add_argument("--all-clips", action="store_true", default=False, help="animate video clips too, not only images")


def _add_global_opts(p: argparse.ArgumentParser, root: bool) -> None:
    """Global flags are accepted both before and after the command name."""
    d = None if root else argparse.SUPPRESS
    p.add_argument("--config", metavar="FILE", default=d, help=f"config file (default: ./{CONFIG_NAME} if present)")
    p.add_argument("--color", dest="color", action="store_true", default=d, help="force colored output")
    p.add_argument("--no-color", dest="color", action="store_false", default=d, help="disable colored output")
    p.add_argument("-q", "--quiet", action="store_true", default=False if root else d, help="only print warnings and errors")
    p.add_argument("-n", "--dry-run", action="store_true", default=False if root else d, help="show what would happen, write nothing")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mltkit",
        description="Beat-sync images and add pan animations to Shotcut .mlt projects.",
        epilog="Put shared options in mltkit.json (see `mltkit init`) so each command needs fewer flags.",
    )
    parser.add_argument("--version", action="version", version=f"mltkit {__version__}")
    _add_global_opts(parser, root=True)

    class _Sub(argparse.ArgumentParser):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            _add_global_opts(self, root=False)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Sub)
    sub.required = True

    p = sub.add_parser("init", help="write a default mltkit.json in the current folder")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("new", help="create starting.mlt from an images folder (+ optional audio)")
    p.add_argument("-o", "--output", metavar="FILE", help="output .mlt (default: starting.mlt)")
    p.add_argument("--images", metavar="DIR", required=False, help="folder with 01.png, 02.png, ...")
    p.add_argument("--pattern", metavar="GLOB")
    p.add_argument("--audio", metavar="FILE", help="voice-over / music file")
    p.add_argument("--duration", type=float, metavar="SEC", help="audio duration when it cannot be probed")
    p.add_argument("--beats", metavar="FILE", help="beat.json - used to infer audio duration")
    p.add_argument("--profile", metavar="NAME", help=f"{', '.join(PROFILES)} or WxH@fps")
    p.add_argument("--image-duration", type=float, metavar="SEC", help="default length of bin images")
    p.add_argument("--absolute-paths", action="store_true", default=None)

    p = sub.add_parser("info", help="inspect a project: profile, tracks, bin, clips")
    p.add_argument("input", nargs="?", metavar="PROJECT.mlt")
    p.add_argument("--clips", action="store_true", help="list every clip on every track")

    p = sub.add_parser("beats", help="show how beat.json maps to frames (no files written)")
    p.add_argument("beats", nargs="?", metavar="beat.json")
    p.add_argument("--input", metavar="PROJECT.mlt", help="project to take the frame rate from")
    p.add_argument("--fps", type=float, help="frame rate when no project is given")
    p.add_argument("--beat-mode", choices=["next-beat", "literal"])
    p.add_argument("--offset", type=float, metavar="SEC")
    p.add_argument("--min-duration", type=int, metavar="FRAMES")
    p.add_argument("--tail", type=float, metavar="SEC")
    p.add_argument("--no-lead-in", dest="lead_in", action="store_false", default=None)

    p = sub.add_parser("sync", help="place images on the timeline according to beat.json")
    p.add_argument("input", nargs="?", metavar="PROJECT.mlt")
    p.add_argument("beats", nargs="?", metavar="beat.json")
    p.add_argument("-o", "--output", metavar="FILE", help="default: after_beat_sync.mlt")
    _add_sync_opts(p)

    p = sub.add_parser("animate", help="add pan/zoom animation to every image clip on a track")
    p.add_argument("input", nargs="?", metavar="PROJECT.mlt")
    p.add_argument("-o", "--output", metavar="FILE", help="default: after_animation.mlt")
    p.add_argument("--video-track", metavar="NAME")
    _add_anim_opts(p)

    p = sub.add_parser("build", help="sync + animate in one go")
    p.add_argument("input", nargs="?", metavar="PROJECT.mlt")
    p.add_argument("beats", nargs="?", metavar="beat.json")
    p.add_argument("--sync-output", metavar="FILE", help="intermediate file (default: after_beat_sync.mlt)")
    p.add_argument("-o", "--output", metavar="FILE", help="final file (default: after_animation.mlt)")
    p.add_argument("--no-intermediate", action="store_true", help="do not write the intermediate sync file")
    _add_sync_opts(p)
    _add_anim_opts(p)

    p = sub.add_parser("clear", help="remove Size/Position animations from a track")
    p.add_argument("input", metavar="PROJECT.mlt")
    p.add_argument("-o", "--output", metavar="FILE", required=True)
    p.add_argument("--video-track", metavar="NAME")

    sub.add_parser("presets", help="list animation presets")
    return parser


# --------------------------------------------------------------- commands


def _print_warnings(ui: UI, warnings: list[str]) -> None:
    for w in warnings:
        ui.warn(w)


def cmd_init(ui: UI, s: Settings, args) -> int:
    path = write_default_config(CONFIG_NAME, force=args.force)
    ui.ok(f"wrote {path}")
    ui.say(ui.dim("Edit it to set your default file names, presets and scale."))
    return 0


def cmd_new(ui: UI, s: Settings, args) -> int:
    images_dir = s.images
    if not images_dir:
        raise MltError("`new` needs --images DIR (or set \"images\" in mltkit.json)")
    profile = resolve_profile(s.profile)
    files = images_for_new(images_dir, s.pattern)
    duration = args.duration
    beats_path = args.beats or (s.beats if Path(str(s.beats)).is_file() else None)
    if s.audio and duration is None and beats_path:
        beats = load_beats(beats_path)
        duration = float(beats[-1].end)
        ui.say(ui.dim(f"audio duration taken from {beats_path}: {pretty_seconds(duration)}"))
    out = s.get("output") or "starting.mlt"
    doc = new_project(out, profile, files, s.audio, duration, s.image_duration, s.absolute_paths)
    ui.heading(f"New project  {profile.description}  {profile.width}x{profile.height} @ {profile.fps_label} fps")
    ui.table(["#", "image"], [[str(i + 1), f.name] for i, f in enumerate(files)])
    if s.audio:
        ui.say("  audio:", Path(s.audio).name)
    if args.dry_run:
        ui.say(ui.dim("dry run - nothing written"))
        return 0
    doc.save(out)
    ui.ok(f"wrote {out}  ({len(files)} images in the Playlist, empty V1/A1)")
    return 0


def cmd_info(ui: UI, s: Settings, args) -> int:
    doc = MltDoc.load(s.input)
    p = doc.profile
    ui.heading(f"{doc.path}")
    ui.say(f"  profile   {p.description}  {p.width}x{p.height} @ {p.fps_label} fps")
    total = doc.timeline_frames()
    ui.say(f"  timeline  {doc.tc(total)}  ({total} frames)")

    bin_items = doc.bin_producers()
    ui.say("")
    ui.heading(f"Playlist / bin  ({len(bin_items)} items)")
    ui.table(
        ["#", "kind", "name", "length"],
        [[str(i + 1), doc.media_kind(pr), doc.caption(pr), doc.tc(doc.producer_frames(pr))] for i, pr in enumerate(bin_items)],
    )

    ui.say("")
    ui.heading("Tracks")
    rows = []
    for t in doc.tracks:
        if t.kind == "background":
            continue
        items = doc.items(t.element)
        clips = [i for i in items if not i.is_blank]
        rows.append([t.name, t.kind, t.id, str(len(clips)), doc.tc(doc.playlist_frames(t.element))])
    ui.table(["name", "kind", "id", "clips", "length"], rows)

    if args.clips:
        producers = doc.producers
        for t in doc.tracks:
            if t.kind == "background":
                continue
            items = [i for i in doc.items(t.element) if not i.is_blank]
            if not items:
                continue
            ui.say("")
            ui.heading(f"{t.name} clips")
            rows = []
            for n, it in enumerate(items, start=1):
                pr = producers.get(it.producer_id or "")
                filters = [doc.prop(f, "shotcut:filter") or doc.prop(f, "mlt_service") or "?" for f in pr.findall("filter")] if pr is not None else []
                rows.append([str(n), doc.caption(pr) if pr is not None else "?", doc.tc(it.start), doc.tc(it.frames), ", ".join(filters)])
            ui.table(["#", "clip", "start", "length", "filters"], rows)
    return 0


def _slots_from_settings(s: Settings, fps):
    beats = load_beats(s.beats)
    slots = build_slots(
        beats,
        fps,
        mode=s.beat_mode,
        offset=float(s.offset),
        min_frames=int(s.min_duration),
        tail=s.tail,
        lead_in=bool(s.lead_in),
    )
    return beats, slots


def cmd_beats(ui: UI, s: Settings, args) -> int:
    if args.input:
        fps = MltDoc.load(args.input).fps
    elif args.fps:
        from fractions import Fraction

        fps = Fraction(str(args.fps))
    else:
        default_input = s.input
        fps = MltDoc.load(default_input).fps if Path(str(default_input)).is_file() else resolve_profile(s.profile).fps
    beats, slots = _slots_from_settings(s, fps)
    ui.heading(f"{len(beats)} beats  mode={s.beat_mode}  fps={fps}")
    rows = [
        [str(i + 1), pretty_seconds(frames_to_seconds(sl.start, fps)), pretty_seconds(frames_to_seconds(sl.frames, fps)), str(sl.frames), sl.beat.text]
        for i, sl in enumerate(slots)
    ]
    ui.table(["#", "start", "length", "frames", "text"], rows)
    return 0


def _run_sync(ui: UI, s: Settings, args, doc: MltDoc) -> None:
    beats, slots = _slots_from_settings(s, doc.fps)
    images = collect_images(doc, s.images, s.pattern, bool(s.bin_order))
    track = doc.find_track(s.video_track, "video")
    report = sync_images(
        doc,
        slots,
        images,
        track,
        overflow=s.overflow,
        keep_filters=not args.strip_filters,
        absolute_paths=bool(s.absolute_paths),
    )
    if s.place_audio:
        try:
            a_track = doc.find_track(s.audio_track, "audio")
            report.audio_placed = place_audio(doc, a_track)
        except MltError as exc:
            report.warnings.append(str(exc))
    total = doc.finalize()

    ui.heading(f"Beat sync  {len(beats)} beats -> {len(report.clips)} clips on {report.track}")
    ui.table(
        ["#", "image", "start", "length", "beat"],
        [[str(c.index), c.caption, doc.tc(c.start), doc.tc(c.frames), c.text] for c in report.clips],
    )
    if report.audio_placed:
        ui.say(f"  audio     {report.audio_placed} placed on {doc.find_track(s.audio_track, 'audio').name}")
    ui.say(f"  timeline  {doc.tc(total)}")
    _print_warnings(ui, report.warnings)


def _run_animate(ui: UI, s: Settings, args, doc: MltDoc) -> None:
    presets = parse_preset_list(s.presets) if s.presets else list(DEFAULT_PRESETS)
    track = doc.find_track(s.video_track, "video")
    report = animate_track(
        doc,
        track,
        presets,
        mode=s.mode,
        scale=float(s.scale),
        seed=s.seed,
        overrides=parse_overrides(args.clip),
        replace=bool(s.replace),
        easing=s.easing,
        images_only=not args.all_clips,
    )
    ui.heading(f"Animate  {len(report.clips)} clips on {report.track}  mode={s.mode} scale={float(s.scale):g} easing={s.easing}")
    ui.table(
        ["#", "image", "preset", "length"],
        [[str(c.index), c.caption, c.preset, doc.tc(c.frames)] for c in report.clips],
    )
    for skipped in report.skipped:
        ui.say(ui.dim(f"  skipped {skipped}"))
    _print_warnings(ui, report.warnings)


def _write(ui: UI, args, doc: MltDoc, out: str) -> None:
    if args.dry_run:
        ui.say(ui.dim(f"dry run - would write {out}"))
        return
    doc.save(out)
    ui.ok(f"wrote {out}")


def cmd_sync(ui: UI, s: Settings, args) -> int:
    doc = MltDoc.load(s.input)
    _run_sync(ui, s, args, doc)
    _write(ui, args, doc, s.get("output") or s.sync_output)
    return 0


def cmd_animate(ui: UI, s: Settings, args) -> int:
    doc = MltDoc.load(s.input)
    _run_animate(ui, s, args, doc)
    _write(ui, args, doc, s.get("output") or s.anim_output)
    return 0


def cmd_build(ui: UI, s: Settings, args) -> int:
    doc = MltDoc.load(s.input)
    _run_sync(ui, s, args, doc)
    if not args.no_intermediate:
        _write(ui, args, doc, args.sync_output or s.sync_output)
    ui.say("")
    _run_animate(ui, s, args, doc)
    _write(ui, args, doc, s.get("output") or s.anim_output)
    return 0


def cmd_clear(ui: UI, s: Settings, args) -> int:
    doc = MltDoc.load(args.input)
    track = doc.find_track(s.video_track, "video")
    n = clear_track_filters(doc, track)
    ui.say(f"removed {n} Size/Position filter(s) from {track.label}")
    _write(ui, args, doc, args.output)
    return 0


def cmd_presets(ui: UI, s: Settings, args) -> int:
    ui.heading("Animation presets")
    ui.table(["name", "description"], [[p.name, p.description] for p in PRESETS.values()], max_width=80)
    ui.say("")
    ui.say(ui.dim("modes: " + ", ".join(MODES) + "    easing: " + ", ".join(EASINGS)))
    return 0


COMMANDS = {
    "init": cmd_init,
    "new": cmd_new,
    "info": cmd_info,
    "beats": cmd_beats,
    "sync": cmd_sync,
    "animate": cmd_animate,
    "build": cmd_build,
    "clear": cmd_clear,
    "presets": cmd_presets,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = UI(color=_use_color(args.color), quiet=args.quiet)
    try:
        config, cfg_path = load_config(args.config)
        if cfg_path and not args.quiet and args.command != "init":
            ui.say(ui.dim(f"using {cfg_path.name}"))
        settings = Settings(args, config)
        return COMMANDS[args.command](ui, settings, args)
    except (MltError, BeatError, FileNotFoundError, FileExistsError, ValueError) as exc:
        ui.error(str(exc))
        return 1
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
