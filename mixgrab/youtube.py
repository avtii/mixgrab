"""YouTube fallback: find a single track on YouTube and save its audio as MP3 (file name gets ' [YT]')."""
from __future__ import annotations

import glob
import logging
import os
import re
from typing import Optional

import yt_dlp

from .soulseek import VERSION_WORDS, norm, strip_parens, tokens
from .tracklist import Track, YDL_EXTRA, run_ydl

log = logging.getLogger("mixgrab")
LONG_WORDS = {"mix", "set", "dj", "podcast", "album", "compilation", "hour", "hours", "megamix", "full",
              "boiler", "festival", "radio", "show", "episode", "ep"}


def _score(e: dict, t: Track) -> Optional[float]:
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
    dur = e.get("duration") or 0
    if dur and (dur < 60 or dur > 20 * 60):
        return None                                    # a snippet or a whole mix, not the track
    if (title_words & LONG_WORDS) - wanted and dur > 12 * 60:
        return None
    if (e.get("channel") or "").endswith(" - Topic"):
        s += 4                                         # YouTube Music "Art Track": clean studio audio
    if re.search(r"official\s+audio", title, re.I):
        s += 2
    if re.search(r"\blive\b|\bcover\b|\breact", title, re.I) and not re.search(r"\blive\b", t.title, re.I):
        s -= 5
    return s


def find(t: Track, results: int = 8) -> Optional[dict]:
    q = f"ytsearch{results}:{t.artist} - {t.title}"
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "extract_flat": True}
    opts.update(YDL_EXTRA)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(q, download=False)
    best, best_s = None, None
    for e in info.get("entries") or []:
        s = _score(e, t)
        log.debug("[%02d] yt %r (%s, %ss) -> %s", t.index, e.get("title"), e.get("channel"), e.get("duration"), s)
        if s is not None and (best_s is None or s > best_s):
            best, best_s = e, s
    return best


def download(entry: dict, dest_noext: str, ffmpeg: str, quality: str = "0",
             progress=lambda done, total: None) -> str:
    """Download the best audio and convert it to MP3 at dest_noext + '.mp3'. Returns that path."""
    url = entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}"
    tmpl = dest_noext + ".yt.%(ext)s"
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
            "format": "bestaudio/best", "outtmpl": tmpl, "ffmpeg_location": os.path.dirname(ffmpeg),
            "retries": 10, "fragment_retries": 10, "socket_timeout": 30, "continuedl": True,
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                "preferredquality": quality},
                               {"key": "FFmpegMetadata"}],
            "progress_hooks": [lambda d: progress(d.get("downloaded_bytes") or 0,
                                                  d.get("total_bytes") or d.get("total_bytes_estimate") or 0)]}
    run_ydl(opts, lambda y: y.download([url]))
    out = dest_noext + ".yt.mp3"
    if not os.path.exists(out):
        raise RuntimeError("YouTube download produced no MP3")
    final = dest_noext + ".mp3"
    os.replace(out, final)
    for leftover in glob.glob(glob.escape(dest_noext) + ".yt.*"):
        try:
            os.remove(leftover)
        except OSError:
            pass
    return final
