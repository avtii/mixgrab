"""The mixgrab engine: the back end shared by the command-line tool and the web app.

Per track:  Soulseek  ->  (not found)  YouTube, then SoundCloud, saved as 'NN - Artist - Title [YT].mp3' / '[SC]'
            then lossless files (FLAC/WAV/AIFF) get an MP3 copy next to them.
Both front ends call Engine.fetch(); the CLI drives one run, the web app keeps one Engine (and one
Soulseek login: the network allows one session per account) for all its jobs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from . import youtube
from .identify import find_ffmpeg
from .soulseek import LOSSLESS, STATUS_CB, Downloader, safe_name
from .tracklist import Track

log = logging.getLogger("mixgrab")
YT_TAG = youtube.SITES["youtube"].tag
DEFAULTS = {
    "convert": {"mp3": True, "bitrate": 320},          # MP3 copy of every lossless download
    "youtube": {"fallback": True, "quality": "0"},     # quality: LAME VBR 0 (best) .. 9, or a bitrate like "320"
    "soundcloud": {"fallback": True},                  # MP3 quality follows [youtube] quality
}


def with_defaults(cfg: dict) -> dict:
    for sect, vals in DEFAULTS.items():
        cfg.setdefault(sect, {})
        for k, v in vals.items():
            cfg[sect].setdefault(k, v)
    return cfg


@dataclass
class Fetched:
    path: Optional[str]          # the downloaded file (FLAC, MP3, ...), None if not found anywhere
    mp3: Optional[str] = None    # MP3 made from a lossless download, next to it
    source: str = ""             # soulseek | youtube | soundcloud | cache
    msg: str = ""                # human-readable detail / reason for failure


def track_prefix(t: Track) -> str:
    return f"{t.index:02d} - {safe_name(t.label)}"


def existing(out_dir: str, t: Track) -> Optional[tuple[str, Optional[str], str]]:
    """Already-downloaded files for a track: (main file, mp3 copy or None, source)."""
    prefix = track_prefix(t)
    try:
        names = os.listdir(out_dir)
    except OSError:
        return None
    mine = [n for n in names if (n.startswith(prefix + ".") or n.startswith(tuple(prefix + tg + "." for tg in youtube.TAGS)))
            and not n.endswith((".part", ".tmp")) and ".yt." not in n]
    if not mine:
        return None
    ext = lambda n: os.path.splitext(n)[1].lstrip(".").lower()
    lossless = [n for n in mine if ext(n) in LOSSLESS]
    mp3 = next((n for n in mine if ext(n) == "mp3"), None)
    j = lambda n: os.path.join(out_dir, n) if n else None
    if lossless:
        return j(lossless[0]), j(mp3), "cache"
    main = mp3 or mine[0]
    return j(main), None, "cache"


class Engine:
    def __init__(self, cfg: dict, dl: Optional[Downloader] = None):
        self.cfg = with_defaults(cfg)
        self.dl = dl
        self.ffmpeg = find_ffmpeg()
        self.on_status = lambda idx, state, **info: None
        self.site_sem = {k: asyncio.Semaphore(2) for k in youtube.SITES}
        self.convert_sem = asyncio.Semaphore(max(1, (os.cpu_count() or 2) // 2))

    def _status(self, idx, state, **info):
        cb = STATUS_CB.get() or self.on_status
        try:
            cb(idx, state, **info)
        except Exception:
            log.debug("status callback failed", exc_info=True)

    @property
    def soulseek_ok(self) -> bool:
        return self.dl is not None and self.dl.client.session is not None

    async def fetch(self, t: Track, out_dir: str) -> Fetched:
        os.makedirs(out_dir, exist_ok=True)
        have = existing(out_dir, t)
        if have:
            path, mp3, _ = have
            if not mp3:
                mp3 = await self._maybe_mp3(t, path)
            return Fetched(path, mp3, "cache", "already downloaded")

        path, msg, source = None, "Soulseek offline", "soulseek"
        if self.soulseek_ok:
            try:
                path, msg = await self.dl.fetch(t, out_dir)
            except Exception as e:
                log.debug("[%02d] soulseek error", t.index, exc_info=True)
                path, msg = None, f"Soulseek error: {e}"

        for site in youtube.SITES.values():
            if path or not self.cfg.get(site.key, {}).get("fallback"):
                continue
            got, why = await self._site(site, t, out_dir)
            if got:
                path, source, msg = got, site.key, f"{why} (Soulseek: {msg})"
            else:
                msg = f"{msg}; {site.name}: {why}"
        if not path:
            return Fetched(None, None, "", msg)
        mp3 = await self._maybe_mp3(t, path)
        return Fetched(path, mp3, source, msg)

    async def _site(self, site, t: Track, out_dir: str) -> tuple[Optional[str], str]:
        self._status(t.index, site.key)
        async with self.site_sem[site.key]:
            try:
                entry = await asyncio.to_thread(youtube.find, t, 8, site.key)
            except Exception as e:
                return None, f"search failed ({str(e)[:120]})"
            if not entry:
                return None, "no match"
            busy = lambda done, total: self._status(t.index, "downloading", done=done, total=total,
                                                    user=site.name, attempt=site.short)
            busy(0, 0)
            dest = os.path.join(out_dir, track_prefix(t) + site.tag)
            path, err = None, None
            for attempt in range(3):                   # whole-download retries on top of yt-dlp's own
                try:
                    path = await asyncio.to_thread(
                        youtube.download, entry, dest, self.ffmpeg, str(self.cfg["youtube"]["quality"]),
                        busy, site.key)
                    break
                except Exception as e:
                    err = e
                    log.debug("[%02d] %s download attempt %d failed", t.index, site.name, attempt + 1, exc_info=True)
                    await asyncio.sleep(5 * (attempt + 1))
            if not path:
                return None, f"download failed ({str(err)[:120]})"
        return path, f"{site.name}: {entry.get('title')} ({entry.get('channel') or entry.get('uploader')})"

    async def _maybe_mp3(self, t: Track, path: str) -> Optional[str]:
        ext = os.path.splitext(path)[1].lstrip(".").lower()
        if not self.cfg["convert"]["mp3"] or ext not in LOSSLESS:
            return None
        self._status(t.index, "converting")
        async with self.convert_sem:
            try:
                return await asyncio.to_thread(to_mp3, self.ffmpeg, path, int(self.cfg["convert"]["bitrate"]))
            except Exception as e:
                log.warning("[%02d] MP3 conversion failed: %s", t.index, e)
                return None


def identify_reason(tl, mode: str, ic: dict) -> Optional[str]:
    """Why the audio should be identified ('auto' | 'always' | 'never'), or None to skip."""
    if mode == "never":
        return None
    if mode == "always":
        return "requested"
    if not tl.tracks:
        return "no tracklist found"
    if tl.looks_incomplete(ic.get("max_avg_track_len", 600)):
        return "tracklist looks incomplete"
    return None


async def identify_into(tl, url: str, work: str, ic: dict,
                        on_audio=lambda done, total: None,
                        on_clips=lambda done, total, matches, wait=0: None) -> tuple[list, bool]:
    """Download the mix audio, fingerprint it and merge confident matches into tl.tracks.
    Answers are kept in `work`, so running again after an interruption resumes the scan.
    Returns (low-confidence hits heard only once, complete). complete is False when Shazam kept
    rate-limiting and the scan stopped early; running again continues it."""
    import json
    from .identify import download_audio, hits_to_tracks, listed_spans, merge, recognizers, scan
    audio = await asyncio.to_thread(download_audio, url, work, on_audio)
    duration = tl.duration
    if not duration:
        ff = find_ffmpeg()
        probe = os.path.join(os.path.dirname(ff), os.path.basename(ff).replace("ffmpeg", "ffprobe"))
        r = await asyncio.to_thread(subprocess.run, [probe, "-v", "error",
                                    "-show_entries", "format=duration", "-of", "csv=p=0", audio],
                                    capture_output=True, text=True)
        try:
            duration = int(float(r.stdout.strip()))
        except ValueError:
            duration = 0
    hits, complete = await scan(audio, duration, ic.get("step", 90), progress=on_clips, fallbacks=recognizers(ic),
                      cache=os.path.join(work, "clips.json"), skip=listed_spans(tl.tracks))
    with open(os.path.join(work, "hits.json"), "w", encoding="utf-8") as f:
        json.dump([h.__dict__ for h in hits], f, indent=1, ensure_ascii=False)
    found, maybe = hits_to_tracks(hits, ic.get("min_hits", 2), duration)
    before = len(tl.tracks)
    tl.tracks = merge(tl.tracks, found)
    tl.found_in += f" + audio ID ({len(tl.tracks) - before} added{'' if complete else ', stopped early'})"
    return maybe, complete


def to_mp3(ffmpeg: str, src: str, bitrate: int = 320) -> str:
    """Encode an MP3 next to src (same name, .mp3), keeping tags and cover art. The original stays."""
    dst = os.path.splitext(src)[0] + ".mp3"
    if os.path.exists(dst):
        return dst
    tmp = dst + ".tmp"
    cmd = [ffmpeg, "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-map", "0:v?", "-c:v", "copy",
           "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-map_metadata", "0", "-id3v2_version", "3",
           "-f", "mp3", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:                              # odd cover-art streams: retry audio only
        cmd = [ffmpeg, "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-c:a", "libmp3lame",
               "-b:a", f"{bitrate}k", "-map_metadata", "0", "-id3v2_version", "3", "-f", "mp3", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    os.replace(tmp, dst)
    return dst
