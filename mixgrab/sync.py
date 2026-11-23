"""Copy the music library to a USB drive (or any folder), sending only what is new or changed."""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field

AUDIO = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".m4a", ".ogg", ".opus", ".wma", ".ape", ".alac"}
LOSSLESS = {".flac", ".wav", ".aiff", ".aif", ".ape", ".alac"}
SKIP_SUFFIX = (".part", ".tmp", ".ytdl")
FAT_SLACK = 2          # FAT/exFAT keep modification times to 2 seconds
# Windows and macOS file names ignore case: "Song.MP3" on the stick is the source's "song.mp3"
FOLD_CASE = os.name == "nt" or sys.platform == "darwin"


def _key(rel: str) -> str:
    return os.path.normcase(rel).casefold() if FOLD_CASE else rel


DRIVE_RE = re.compile(r"[A-Za-z]:?[\\/]?")


def resolve_source(src: str) -> str:
    """A folder, or a bare drive letter ('E', 'E:') for the whole drive."""
    s = src.strip().strip('"')
    if DRIVE_RE.fullmatch(s):
        return s[0].upper() + ":\\"
    return os.path.abspath(os.path.expandvars(os.path.expanduser(s)))


def resolve_dest(dest: str, source: str) -> str:
    """'D', 'D:' or 'D:\\' mean a folder named like the source on that drive (the drive's root when
    the source is itself a whole drive); anything else is used as is."""
    d = dest.strip().strip('"')
    if DRIVE_RE.fullmatch(d):
        return os.path.join(d[0].upper() + ":\\", os.path.basename(os.path.normpath(source)))
    return os.path.abspath(os.path.expandvars(os.path.expanduser(d)))


def overlaps(a: str, b: str) -> bool:
    """True when one folder is inside the other (or they are the same), links and junctions followed:
    syncing then would copy the destination into itself, or a mirror would delete the source."""
    ra, rb = (os.path.normcase(os.path.realpath(x)) for x in (a, b))
    if os.path.splitdrive(ra)[0] != os.path.splitdrive(rb)[0]:
        return False
    return os.path.commonpath([ra, rb]) in (ra, rb)


@dataclass
class Plan:
    copy: list[tuple[str, str, int]] = field(default_factory=list)   # (src, dst, size)
    same: int = 0
    remove: list[str] = field(default_factory=list)                   # with mirror: files only on the drive
    playlists: list[tuple[str, str]] = field(default_factory=list)    # m3u to rewrite for mp3-only
    source: str = ""
    dest: str = ""
    mp3_only: bool = False
    # filled in by run()
    copied: int = 0
    removed: int = 0
    rewritten: int = 0
    kept: list[str] = field(default_factory=list)   # planned removals that run() refused (see _may_remove)

    @property
    def bytes(self) -> int:
        return sum(s for _, _, s in self.copy)


def _wanted(root: str, mp3_only: bool) -> dict[str, str]:
    """Relative path -> source path for every library file worth copying."""
    out = {}
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))     # .identify scratch, hidden folders
        names = set(files)
        for n in sorted(files):
            if n.startswith(".") or n.endswith(SKIP_SUFFIX) or ".yt." in n:
                continue
            stem, ext = os.path.splitext(n)
            if mp3_only and ext.lower() in LOSSLESS and f"{stem}.mp3" in names:
                continue                                    # its MP3 copy goes instead
            src = os.path.join(d, n)
            out[os.path.relpath(src, root)] = src
    return out


def _unchanged(src: str, dst: str) -> bool:
    try:
        a, b = os.stat(src), os.stat(dst)
    except OSError:
        return False
    return a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) <= FAT_SLACK


def plan(library: str, dest: str, mp3_only: bool = False, mirror: bool = False) -> Plan:
    if overlaps(library, dest):
        raise ValueError("the source and the destination overlap")
    if mirror and os.path.islink(dest):
        raise ValueError("won't mirror into a destination that is a link")
    p = Plan(source=library, dest=dest, mp3_only=mp3_only)
    want = _wanted(library, mp3_only)
    want_keys = {_key(rel) for rel in want}
    for rel, src in want.items():
        dst = os.path.join(dest, rel)
        if mp3_only and rel.lower().endswith(".m3u"):
            p.playlists.append((src, dst))
            continue
        if _unchanged(src, dst):
            p.same += 1
        else:
            p.copy.append((src, dst, os.path.getsize(src)))
    if mirror and os.path.isdir(dest):
        for d, dirs, files in os.walk(dest):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for n in files:
                full = os.path.join(d, n)
                if _key(os.path.relpath(full, dest)) not in want_keys:
                    p.remove.append(full)
    return p


def _may_remove(p: Plan, f: str) -> bool:
    """Last check before a mirror deletes f: it is really inside the destination (no link leads out),
    and the source still has no file for it. The plan may be minutes old by the time someone says yes."""
    dest = p.dest
    if not dest or not p.source or overlaps(p.source, dest):
        return False
    real_dest = os.path.normcase(os.path.realpath(dest))
    real_dir = os.path.normcase(os.path.realpath(os.path.dirname(f)))
    try:
        inside = os.path.commonpath([real_dest, real_dir]) == real_dest
    except ValueError:                              # different drives: a junction leads elsewhere
        inside = False
    if not inside or os.path.islink(f):
        return False
    rel = os.path.relpath(f, dest)
    if rel.startswith(os.pardir):
        return False
    src = os.path.join(p.source, rel)
    if os.path.exists(src):
        stem, ext = os.path.splitext(src)
        # with --mp3-only a lossless file is replaced by its MP3 copy, so it still goes
        return p.mp3_only and ext.lower() in LOSSLESS and os.path.exists(stem + ".mp3")
    return True


def _mp3_playlist(src: str, dst: str):
    """The library playlist with lossless entries pointed at their MP3 copies."""
    folder = os.path.dirname(src)
    lines = []
    # surrogateescape: a playlist saved in another encoding passes through byte for byte
    with open(src, encoding="utf-8", errors="surrogateescape") as f:
        for line in f.read().splitlines():
            stem, ext = os.path.splitext(line)
            if line and not line.startswith("#") and ext.lower() in LOSSLESS \
                    and os.path.exists(os.path.join(folder, stem + ".mp3")):
                line = stem + ".mp3"
            lines.append(line)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write("\n".join(lines) + "\n")


CHUNK = 256 * 1024     # small writes leave gaps on a shared USB controller for the mouse and keyboard


def go_easy() -> None:
    """Give way to everything else on the machine: lowest I/O and CPU priority for this process.
    A copy at full speed can starve a mouse or keyboard on the same USB controller."""
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00100000)   # PROCESS_MODE_BACKGROUND_BEGIN: low I/O + CPU
        else:
            os.nice(10)
    except (OSError, AttributeError):
        pass


class Pace:
    """Caps the copy at `mb_s` megabytes a second (None or 0: no cap). No catching up after a pause."""

    def __init__(self, mb_s: float | None):
        self.rate = (mb_s or 0) * 1e6
        self.next = time.monotonic()

    def wrote(self, n: int) -> None:
        if not self.rate:
            return
        now = time.monotonic()
        self.next = max(self.next, now) + n / self.rate
        if self.next > now:
            time.sleep(self.next - now)


def _copy(src: str, dst: str, pace: Pace, tick) -> None:
    """shutil.copy2 in small, paced chunks (times and permissions kept, so the next sync sees it unchanged)."""
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while chunk := fi.read(CHUNK):
            fo.write(chunk)
            tick(len(chunk))
            pace.wrote(len(chunk))
    shutil.copystat(src, dst)


def run(p: Plan, dest: str, progress=lambda done_bytes, name: None, speed: float | None = None) -> list[tuple[str, str]]:
    """Carry out a plan, at most `speed` MB/s if given. Returns (file, error) for files that failed;
    the rest still go."""
    errors = []
    done = 0
    pace = Pace(speed)
    for src, dst, size in p.copy:
        name = os.path.basename(src)
        progress(done, name)
        tmp = dst + ".part"
        got = 0

        def tick(n, base=done):
            nonlocal got
            got += n
            progress(base + min(got, size), name)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            _copy(src, tmp, pace, tick)             # a pulled stick leaves a .part, never a cut-off song
            os.replace(tmp, dst)
            p.copied += 1
        except OSError as e:
            errors.append((src, e.strerror or str(e)))
            try:
                os.remove(tmp)
            except OSError:
                pass
        done += size
        progress(done, os.path.basename(src))
    for src, dst in p.playlists:
        try:
            _mp3_playlist(src, dst)
            p.rewritten += 1
        except (OSError, ValueError) as e:          # one odd playlist mustn't stop the rest
            errors.append((src, getattr(e, "strerror", None) or str(e)))
    for f in p.remove:
        if not _may_remove(p, f):
            p.kept.append(f)
            continue
        try:
            os.remove(f)
            p.removed += 1
        except OSError as e:
            errors.append((f, e.strerror or str(e)))
    if p.remove:                                    # drop folders the mirror emptied
        for d in sorted((w[0] for w in os.walk(dest)), key=len, reverse=True):
            if d != dest and not os.listdir(d):
                try:
                    os.rmdir(d)
                except OSError:
                    pass
    return errors
