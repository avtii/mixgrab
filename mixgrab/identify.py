"""Identify the songs in a mix from its audio (Shazam fingerprints of short clips)."""
from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass

import yt_dlp
from shazamio import Shazam

from .soulseek import norm
from .tracklist import Track

log = logging.getLogger("mixgrab")
CLIP = 12  # Shazam's window; >=15 s returns no matches


def find_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    pats = [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe")]
    for p in pats:
        hits = glob.glob(p)
        if hits:
            return hits[0]
    raise SystemExit("ffmpeg not found. Install it: winget install Gyan.FFmpeg")


def download_audio(url: str, work_dir: str, progress=lambda done, total: None) -> str:
    os.makedirs(work_dir, exist_ok=True)
    existing = glob.glob(os.path.join(work_dir, "mixaudio.*"))
    if existing:
        return existing[0]
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": "bestaudio/best",
            "outtmpl": os.path.join(work_dir, "mixaudio.%(ext)s"), "noplaylist": True,
            "progress_hooks": [lambda d: progress(d.get("downloaded_bytes") or 0,
                                                  d.get("total_bytes") or d.get("total_bytes_estimate") or 0)]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def clip(ffmpeg: str, src: str, at: int) -> bytes:
    r = subprocess.run([ffmpeg, "-v", "error", "-ss", str(at), "-t", str(CLIP), "-i", src,
                        "-ac", "1", "-ar", "16000", "-f", "wav", "-"],
                       capture_output=True, check=True)
    return r.stdout


@dataclass
class Hit:
    at: int
    artist: str
    title: str

    @property
    def key(self) -> str:
        return norm(f"{self.artist} {self.title}")


async def scan(audio: str, duration: int, step: int, parallel: int = 3,
               progress=lambda done, total, matches: None) -> list[Hit]:
    ffmpeg = find_ffmpeg()
    points = list(range(5, max(duration - CLIP, 6), step))
    log.debug("Identifying: %d clips of %ds, every %ds", len(points), CLIP, step)
    sem = asyncio.Semaphore(parallel)
    shazam = Shazam()
    hits: list[Hit] = []
    done = 0

    async def one(at: int):
        nonlocal done
        async with sem:
            try:
                data = await asyncio.to_thread(clip, ffmpeg, audio, at)
                for attempt in range(3):
                    try:
                        res = await shazam.recognize(data)
                        break
                    except Exception as e:  # rate limits / transient HTTP errors
                        if attempt == 2:
                            raise
                        await asyncio.sleep(5 * (attempt + 1))
                tr = res.get("track")
                if tr:
                    hits.append(Hit(at, tr.get("subtitle", "").strip(), tr.get("title", "").strip()))
            except Exception as e:
                log.debug("clip at %ds failed: %s", at, e)
            done += 1
            progress(done, len(points), len(hits))

    await asyncio.gather(*(one(p) for p in points))
    hits.sort(key=lambda h: h.at)
    return hits


def hits_to_tracks(hits: list[Hit], min_hits: int, duration: int) -> tuple[list[Track], list[Hit]]:
    """Group matches by song. Returns (confident tracks, low-confidence one-off hits)."""
    groups: dict[str, list[Hit]] = {}
    for h in hits:
        groups.setdefault(h.key, []).append(h)
    good, maybe = [], []
    for key, hs in groups.items():
        if len(hs) >= min_hits:
            good.append(hs)
        else:
            maybe.extend(hs)
    good.sort(key=lambda hs: hs[0].at)
    tracks = []
    for i, hs in enumerate(good):
        start = hs[0].at
        nxt = good[i + 1][0].at if i + 1 < len(good) else duration
        tracks.append(Track(i + 1, hs[0].artist, hs[0].title, start, max(nxt - start, 60)))
    return tracks, maybe


def merge(listed: list[Track], found: list[Track]) -> list[Track]:
    """Combine a partial tracklist with identified tracks, dropping duplicates."""
    def same(a: Track, b: Track) -> bool:
        ta, tb = set(norm(a.title).split()), set(norm(b.title).split())
        return bool(ta and tb) and len(ta & tb) / min(len(ta), len(tb)) >= 0.6

    out = list(listed)
    for f in found:
        if not any(same(f, t) for t in out):
            out.append(f)
    out.sort(key=lambda t: (t.start is None, t.start or 0))
    for i, t in enumerate(out):
        t.index = i + 1
    return out
