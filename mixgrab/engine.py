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
from .soulseek import LOSSLESS, PARTIAL, STATUS_CB, Downloader, track_prefix
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


FFMPEG_TIMEOUT = 15 * 60          # a single encode of even a long FLAC takes well under this


def _nonempty(p: str) -> bool:
    try:
        return os.path.getsize(p) > 0
    except OSError:
        return False


def existing(out_dir: str, t: Track) -> Optional[tuple[str, Optional[str], str]]:
    """Already-downloaded files for a track: (main file, mp3 copy or None, source)."""
    prefix = track_prefix(t)
    try:
        names = os.listdir(out_dir)
    except OSError:
        return None
    mine = [n for n in names if (n.startswith(prefix + ".") or n.startswith(tuple(prefix + tg + "." for tg in youtube.TAGS)))
            and not n.endswith(PARTIAL) and ".yt." not in n
            and _nonempty(os.path.join(out_dir, n))]
    if not mine:
        return None
    ext = lambda n: os.path.splitext(n)[1].lstrip(".").lower()
    lossless = [n for n in mine if ext(n) in LOSSLESS]
    j = lambda n: os.path.join(out_dir, n) if n else None
    if lossless:
        # its MP3 copy is exactly 'prefix.mp3': an older '[YT]'/'[SC]' MP3 is a different recording
        mp3 = prefix + ".mp3" if prefix + ".mp3" in mine else None
        return j(lossless[0]), j(mp3), "cache"
    mp3 = next((n for n in mine if ext(n) == "mp3"), None)
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
            entry, err = None, None
            for attempt in range(3):                   # ride out a short network or DNS outage
                try:
                    entry, err = await asyncio.to_thread(youtube.find, t, 8, site.key), None
                    break
                except Exception as e:
                    err = e
                    log.debug("[%02d] %s search attempt %d failed", t.index, site.name, attempt + 1, exc_info=True)
                    if attempt == 2 or not transient(e):
                        break
                    await asyncio.sleep((5, 20)[attempt])
            if err:
                return None, f"search failed ({offline_hint(err)})"
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
                    youtube.remove_partials(dest)      # a broken half-download mustn't poison the retry
                    if attempt == 2 or not transient(e):
                        break                          # no point waiting to fail the same way again
                    await asyncio.sleep(5 * (attempt + 1))
            if not path:
                return None, f"download failed ({offline_hint(err)})"
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


_TRANSIENT = ("timed out", "timeout", "connection", "temporarily", "reset by peer", "incompleteread",
              "http error 5", "http error 429", "too many requests", "unable to download", "network",
              "eof occurred", "remote end closed", "fragment")
# a failed name lookup: the connection or the router's DNS dropped for a moment
_DNS = ("getaddrinfo", "name resolution", "nodename nor servname", "name or service not known",
        "no address associated")


def transient(e: BaseException) -> bool:
    """Worth retrying (network trouble), as opposed to a private/removed video, a bot check or bad settings."""
    s = str(e).lower()
    return any(w in s for w in _TRANSIENT + _DNS)


def offline_hint(e: BaseException) -> str:
    """The error for a track's status; for a failed name lookup, say what it means."""
    s = str(e)
    if any(w in s.lower() for w in _DNS):
        return "no internet: couldn't look up the site's address"
    return s.replace("ERROR: ", "")[:120]


def find_ffprobe(ffmpeg: str) -> Optional[str]:
    """ffprobe next to ffmpeg (ffmpeg.exe -> ffprobe.exe, keeping the folder), else on PATH."""
    import shutil
    folder, name = os.path.split(ffmpeg)
    cand = os.path.join(folder, "ffprobe" + os.path.splitext(name)[1])
    return cand if os.path.isfile(cand) else shutil.which("ffprobe")


def probe_duration(audio: str) -> int:
    """Length of an audio file in whole seconds, 0 if it can't be read."""
    probe = find_ffprobe(find_ffmpeg())
    if not probe:
        return 0
    try:
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", audio],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return 0
    for line in r.stdout.splitlines():                # first numeric line; 'N/A' when unknown
        try:
            return max(0, int(float(line.strip())))
        except ValueError:
            continue
    return 0


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
    duration = tl.duration or await asyncio.to_thread(probe_duration, audio)
    if not duration:                                   # one clip at 0:00 would identify nothing
        log.warning("couldn't tell how long the mix audio is; skipping audio ID")
        tl.found_in += " (audio ID skipped: unknown length)"
        return [], True
    try:
        step = max(10, int(ic.get("step", 90)))          # 0 or junk in the config: fall back to sane steps
    except (TypeError, ValueError):
        step = 90
    hits, complete = await scan(audio, duration, step, progress=on_clips, fallbacks=recognizers(ic),
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
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    tmp = dst + ".tmp"

    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 1, "", "ffmpeg took too long")

    r = run([ffmpeg, "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-map", "0:v?", "-c:v", "copy",
             "-c:a", "libmp3lame", "-b:a", f"{bitrate}k", "-map_metadata", "0", "-id3v2_version", "3",
             "-f", "mp3", tmp])
    if r.returncode != 0:                              # odd cover-art streams: retry audio only
        r = run([ffmpeg, "-v", "error", "-y", "-i", src, "-map", "0:a:0", "-c:a", "libmp3lame",
                 "-b:a", f"{bitrate}k", "-map_metadata", "0", "-id3v2_version", "3", "-f", "mp3", tmp])
    if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise RuntimeError(r.stderr.strip()[-300:] or "ffmpeg failed")
    os.replace(tmp, dst)
    return dst
