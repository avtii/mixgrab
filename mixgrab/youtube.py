"""Fallback sources for tracks Soulseek lacks: find a single track on YouTube or SoundCloud and save its
audio as MP3. The file name gets the site's tag: ' [YT]' or ' [SC]'."""
from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

import yt_dlp

from .soulseek import VERSION_WORDS, norm, strip_parens, tokens
from .tracklist import Track, run_ydl, ydl_extra

log = logging.getLogger("mixgrab")
LONG_WORDS = {"mix", "set", "dj", "podcast", "album", "compilation", "hour", "hours", "megamix", "full",
              "boiler", "festival", "radio", "show", "episode", "ep"}
SHORT_RE = re.compile(r"\b(clip|snippet|teaser|preview|trailer|sample)\b", re.I)
MASHUP_RE = re.compile(r"\s(x|vs\.?|mashup)\s|\bmash-?up\b", re.I)


@dataclass(frozen=True)
class Site:
    key: str        # config section and Fetched.source
    name: str       # shown to people
    short: str      # file tag / badge
    search: str     # yt-dlp search prefix

    @property
    def tag(self) -> str:
        return f" [{self.short}]"


SITES = {s.key: s for s in (Site("youtube", "YouTube", "YT", "ytsearch"),
                            Site("soundcloud", "SoundCloud", "SC", "scsearch"))}
TAGS = tuple(s.tag for s in SITES.values())


def _score(e: dict, t: Track, site: Site = SITES["youtube"]) -> Optional[float]:
    title = e.get("title") or ""
    channel = re.sub(r"\s*-\s*Topic$", "", e.get("channel") or e.get("uploader") or "")
    have = set(norm(f"{title} {channel}").split())
    title_words = set(norm(title).split())
    want_title = tokens(t.title) or norm(t.title).split()
    want_artist = tokens(t.artist) or norm(t.artist).split()
    core = tokens(strip_parens(t.title)) or want_title
    if not want_title or not all(w in have for w in core):
        return None
    tf = sum(w in have for w in want_title) / len(want_title)
    af = sum(w in have for w in want_artist) / len(want_artist) if want_artist else 1
    if tf < 0.75 or af < 0.5:
        return None
    s = 10 * tf + 5 * af
    wanted = set(norm(t.title).split())
    s -= 4 * len((title_words & VERSION_WORDS) - wanted)
    dur = e.get("duration")
    dur = dur if isinstance(dur, (int, float)) else 0   # flat search results sometimes leave it out or odd
    if dur and (dur < 60 or dur > 20 * 60):
        return None                                    # a snippet (or SoundCloud 30 s preview), or a whole mix
    if (title_words & LONG_WORDS) - wanted and dur > 12 * 60:
        return None
    if (e.get("channel") or "").endswith(" - Topic"):
        s += 4                                         # YouTube Music "Art Track": clean studio audio
    if SHORT_RE.search(title) and not SHORT_RE.search(t.title):
        return None                                    # a clip / snippet / teaser, not the full track
    if site.key == "soundcloud":
        if want_artist and set(want_artist) <= set(norm(channel).split()):
            s += 3                                     # uploaded by the artist's own account
        plain = set(norm(re.sub(r"original\s+mix", "", title, flags=re.I)).split())
        if (plain & (VERSION_WORDS | {"flip", "vip", "mashup"})) - wanted:
            return None                                # SoundCloud is full of fan edits/remixes: exact only
        song = re.split(r"\s+[-–—]\s+", title, maxsplit=1)[-1]     # "A x B - Song" is a collab credit
        if MASHUP_RE.search(song) and not MASHUP_RE.search(t.title):
            return None                                # "A x B" / "A vs B" mashup
    if re.search(r"official\s+audio", title, re.I):
        s += 2
    if re.search(r"\blive\b|\bcover\b|\breact", title, re.I) and not re.search(r"\blive\b", t.title, re.I):
        s -= 5
    return s


def find(t: Track, results: int = 8, site: str = "youtube") -> Optional[dict]:
    st = SITES[site]
    q = f"{st.search}{results}:{t.artist} - {t.title}"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
    opts.update(ydl_extra())
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(q, download=False)
    best, best_s = None, None
    for e in (info or {}).get("entries") or []:
        if not isinstance(e, dict) or not (e.get("url") or e.get("id")):
            continue                                   # dead or malformed result
        s = _score(e, t, st)
        log.debug("[%02d] %s %r (%s, %ss) -> %s", t.index, st.short, e.get("title"),
                  e.get("channel") or e.get("uploader"), e.get("duration"), s)
        if s is not None and (best_s is None or s > best_s):
            best, best_s = e, s
    return best


def _no_previews(ctx):
    """yt-dlp format selector: never settle for a SoundCloud 30-second preview."""
    fmts = [f for f in ctx.get("formats") or [] if "preview" not in (f.get("format_id") or "")
            and f.get("acodec") != "none" and f.get("ext") != "mhtml"]
    if not fmts:
        return
    # known audio-only formats first (acodec None means unknown, which may be a video), then bitrate
    yield max(fmts, key=lambda f: (f.get("acodec") is not None, f.get("vcodec") in (None, "none"),
                                   f.get("abr") or f.get("tbr") or 0, f.get("quality") or 0))


def download(entry: dict, dest_noext: str, ffmpeg: str, quality: str = "0",
             progress=lambda done, total: None, site: str = "youtube") -> str:
    """Download the best audio and convert it to MP3 at dest_noext + '.mp3'. Returns that path."""
    url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    tmpl = dest_noext + ".yt.%(ext)s"
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "format": _no_previews if site == "soundcloud" else "bestaudio/best",
            "outtmpl": tmpl, "ffmpeg_location": os.path.dirname(ffmpeg),
            "retries": 10, "fragment_retries": 10, "socket_timeout": 30, "continuedl": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": quality},
                               {"key": "FFmpegMetadata"}],
            "progress_hooks": [lambda d: progress(d.get("downloaded_bytes") or 0,
                                                  d.get("total_bytes") or d.get("total_bytes_estimate") or 0)]}
    try:
        run_ydl(opts, lambda y: y.download([url]))
        out = dest_noext + ".yt.mp3"
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            raise RuntimeError(f"{SITES[site].name} download produced no MP3")
        final = dest_noext + ".mp3"
        os.replace(out, final)
        return final
    finally:
        remove_partials(dest_noext)


def remove_partials(dest_noext: str):
    """Delete the '.yt.*' working files of a download (finished or not)."""
    for leftover in glob.glob(glob.escape(dest_noext) + ".yt.*"):
        try:
            os.remove(leftover)
        except OSError:
            pass
