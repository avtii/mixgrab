"""Find the tracklist of a YouTube mix / DJ set / playlist.

Sources, in order of trust:
  1. YouTube playlist entries (when the URL is a playlist)
  2. Video chapters
  3. Video description
  4. Comments (uploader's / pinned first, then the one with the most tracks)
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from typing import Optional

import yt_dlp


@dataclass
class Track:
    index: int
    artist: str
    title: str
    start: Optional[int] = None      # seconds into the mix
    length: Optional[int] = None     # approximate length in seconds

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class Tracklist:
    source_title: str
    found_in: str
    tracks: list[Track] = field(default_factory=list)
    duration: Optional[int] = None
    unknown: int = 0          # "ID - ID" style placeholder lines in the source

    def looks_incomplete(self, max_avg_len: int = 600) -> bool:
        if not self.tracks:
            return True
        if self.unknown:
            return True
        if self.found_in == "playlist entries" or not self.duration:
            return False
        return self.duration / len(self.tracks) > max_avg_len


TS_RE = re.compile(r"[\[(]?\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b[\])]?")
NUM_RE = re.compile(r"^\s*(?:#|no\.?\s*)?\d{1,3}\s*[.)\]:]\s*|^\s*0\d\s+(?=\S)", re.I)  # bare "01 " only when zero-padded (keeps "50 Cent")
SEP_RE = re.compile(r"\s+[-–—~|]\s+|\s*[–—]\s*")
LEAD_JUNK_RE = re.compile(r"^[^\w(\[]+", re.UNICODE)
TRAIL_JUNK_RE = re.compile(r"[\s\-–—|:,.]+$")
VIDEO_JUNK_RE = re.compile(
    r"\s*[\[(](?:official\s*)?(?:music\s*)?(?:video|audio|visuali[sz]er|lyrics?(?:\s*video)?|hd|hq|4k|"
    r"full\s*album|premiere|clip|out\s*now)[^\])]*[\])]",
    re.I,
)
UNKNOWN = {"id", "unknown", "?", "??", "???", "tba", "unreleased"}
ID_LINE_RE = re.compile(r"(?:^|[\s\-–—.)])(?:ID|\?\?+)\s*[-–—]\s*(?:ID|\?\?+)(?:\s|$)")


def count_unknown(text: str) -> int:
    return sum(bool(ID_LINE_RE.search(l)) for l in text.splitlines())


def _ts_to_sec(h, m, s) -> int:
    return int(h or 0) * 3600 + int(m) * 60 + int(s)


def parse_line(line: str) -> Optional[tuple[str, str, Optional[int]]]:
    """Parse one 'NN. Artist - Title 1:23:45'-style line. Returns (artist, title, start)."""
    if not line or "http" in line.lower() or len(line) > 200:
        return None
    start = None
    m = TS_RE.search(line)
    if m:
        start = _ts_to_sec(*m.groups())
        line = line[: m.start()] + " " + line[m.end():]
    line = LEAD_JUNK_RE.sub("", line.strip())
    line = NUM_RE.sub("", line)
    line = LEAD_JUNK_RE.sub("", line)
    line = TRAIL_JUNK_RE.sub("", line).strip()
    parts = [p.strip() for p in SEP_RE.split(line, maxsplit=1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    artist, title = parts
    # "01 - Artist - Title" leftover
    if artist.isdigit():
        sub = [p.strip() for p in SEP_RE.split(title, maxsplit=1)]
        if len(sub) != 2:
            return None
        artist, title = sub
    title = VIDEO_JUNK_RE.sub("", title).strip()
    title = TRAIL_JUNK_RE.sub("", title).strip()
    if not artist or not title or len(artist) > 100:
        return None
    if artist.lower() in UNKNOWN and title.lower() in UNKNOWN:
        return None
    return artist, title, start


def parse_block(text: str) -> list[Track]:
    tracks: list[Track] = []
    for raw in text.splitlines():
        p = parse_line(raw)
        if p:
            tracks.append(Track(len(tracks) + 1, *p))
    return tracks


def _fill_lengths(tracks: list[Track], total: Optional[int]) -> None:
    for i, t in enumerate(tracks):
        if t.start is None or t.length is not None:
            continue
        nxt = tracks[i + 1].start if i + 1 < len(tracks) else total
        if nxt is not None and nxt > t.start:
            t.length = nxt - t.start


def _good(tracks: list[Track]) -> bool:
    if len(tracks) >= 3:
        return True
    return len(tracks) >= 2 and all(t.start is not None for t in tracks)


# Extra yt-dlp options applied everywhere (e.g. {"cookiefile": "..."} on servers YouTube distrusts)
YDL_EXTRA: dict = {}
if not shutil.which("deno") and shutil.which("node"):
    YDL_EXTRA["js_runtimes"] = {"node": {}}   # YouTube needs a JS runtime; yt-dlp only enables deno by default


def _ydl(extra: dict) -> yt_dlp.YoutubeDL:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noprogress": True}
    opts.update(YDL_EXTRA)
    opts.update(extra)
    return yt_dlp.YoutubeDL(opts)


def fetch_tracklist(url: str, log=print, online: bool = True) -> Tracklist:
    with _ydl({"extract_flat": "in_playlist"}) as ydl:
        info = ydl.extract_info(url, download=False)
    tl = _from_youtube(url, info, log)
    if online and info.get("_type") != "playlist" and tl.looks_incomplete():
        from .lookup import lookup
        found = lookup(info, log)
        if found and (len(found.tracks) > len(tl.tracks)
                      or (getattr(found, "verified", False) and len(found.tracks) >= 0.6 * len(tl.tracks))
                      or (len(found.tracks) == len(tl.tracks) and found.unknown < tl.unknown)):
            return found
    return tl


def _from_youtube(url: str, info: dict, log) -> Tracklist:
    title = info.get("title") or url

    # 1. Playlist: each entry title is a track
    if info.get("_type") == "playlist":
        tracks = []
        for e in info.get("entries") or []:
            p = parse_line(e.get("title") or "")
            if p:
                tracks.append(Track(len(tracks) + 1, p[0], p[1], None, e.get("duration")))
            elif e.get("title") and e.get("channel"):
                # "Title" uploaded by "Artist - Topic" (YouTube Music auto channels)
                ch = re.sub(r"\s*-\s*Topic$", "", e["channel"])
                tracks.append(Track(len(tracks) + 1, ch, VIDEO_JUNK_RE.sub("", e["title"]).strip(),
                                    None, e.get("duration")))
        return Tracklist(title, "playlist entries", tracks)

    total = info.get("duration")

    # 2. Chapters
    chapters = info.get("chapters") or []
    tracks = []
    for ch in chapters:
        p = parse_line(ch.get("title") or "")
        if p:
            st, en = int(ch.get("start_time") or 0), ch.get("end_time")
            tracks.append(Track(len(tracks) + 1, p[0], p[1], st, int(en - st) if en else None))
    if _good(tracks):
        return Tracklist(title, "chapters", tracks, total,
                         sum(count_unknown(c.get("title") or "") for c in chapters))

    # 3. Description
    tracks = parse_block(info.get("description") or "")
    if _good(tracks):
        _fill_lengths(tracks, total)
        return Tracklist(title, "description", tracks, total, count_unknown(info.get("description") or ""))

    # 3b. Single-artist album: chapters / timestamped lines carry titles only ("2. In Suspense 4:25");
    #     the artist comes from the video title ("Artist - Album [Full Album]") or the channel.
    artist = album_artist(info)
    if artist:
        tracks = []
        for ch in chapters:
            name = title_only(ch.get("title") or "")
            if name:
                st, en = int(ch.get("start_time") or 0), ch.get("end_time")
                tracks.append(Track(len(tracks) + 1, artist, name, st, int(en - st) if en else None))
        if _good(tracks):
            return Tracklist(title, f"chapters (album by {artist})", tracks, total)
        tracks = []
        for line in (info.get("description") or "").splitlines():
            if TS_RE.search(line) and "http" not in line.lower():
                name = title_only(line)
                if name:
                    m = TS_RE.search(line)
                    tracks.append(Track(len(tracks) + 1, artist, name, _ts_to_sec(*m.groups())))
        if _good(tracks) and all(t.start is not None for t in tracks):
            _fill_lengths(tracks, total)
            return Tracklist(title, f"description (album by {artist})", tracks, total)

    # 4. Comments
    log("No tracklist in chapters/description, reading comments...")
    with _ydl({"getcomments": True,
               "extractor_args": {"youtube": {"max_comments": ["400", "all", "100"],
                                              "comment_sort": ["top"]}}}) as ydl:
        info = ydl.extract_info(url, download=False)
    best, best_key = None, None
    for c in info.get("comments") or []:
        tr = parse_block(c.get("text") or "")
        if not _good(tr):
            continue
        key = (bool(c.get("author_is_uploader") or c.get("is_pinned")), len(tr), c.get("like_count") or 0)
        if best_key is None or key > best_key:
            best, best_key = (tr, c), key
    if best:
        tr, c = best
        _fill_lengths(tr, total)
        return Tracklist(title, f"comment by {c.get('author')}", tr, total, count_unknown(c.get("text") or ""))

    return Tracklist(title, "nothing found", [], total)


VA_RE = re.compile(r"^(va|v\.a\.|various( artists)?|mix|dj mix|playlist)$", re.I)


def album_artist(info: dict) -> Optional[str]:
    """Artist of a single-artist album upload, from 'Artist - Album [Full Album]' or the channel."""
    if re.search(r"compiled by|various artists|\bv\.?a\.?(?=\s|$)|mixed by|\bmix\b|\bdj set\b", info.get("title") or "", re.I):
        return None
    t = VIDEO_JUNK_RE.sub("", info.get("title") or "")
    t = re.sub(r"^\s*[^\w(]*\s*(?:\w+\s*)?(?:psychill|psybient|chillout|ambient)\s*[-|:]\s*", "", t, flags=re.I) \
        if t.count(" - ") >= 2 else t     # "Genre - Artist - Album" channel style
    parts = [p.strip() for p in SEP_RE.split(t, maxsplit=1)]
    if len(parts) == 2 and parts[0] and not VA_RE.match(parts[0]) and len(parts[0]) <= 60:
        return parts[0]
    ch = info.get("artist") or info.get("creator") or ""
    if not ch and (info.get("channel") or "").endswith(" - Topic"):
        ch = info["channel"][:-8]
    return ch.strip() or None


def title_only(line: str) -> Optional[str]:
    line = TS_RE.sub(" ", line)
    line = LEAD_JUNK_RE.sub("", line.strip())
    line = NUM_RE.sub("", line)
    line = LEAD_JUNK_RE.sub("", line)
    line = TRAIL_JUNK_RE.sub("", line).strip()
    if not line or len(line) > 120 or line.lower() in UNKNOWN:
        return None
    return line


def load_tracklist_file(path: str, title: str) -> Tracklist:
    with open(path, encoding="utf-8") as f:
        tracks = parse_block(f.read())
    _fill_lengths(tracks, None)
    return Tracklist(title, f"file {path}", tracks)
