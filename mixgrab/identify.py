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


FFMPEG_HINT = {"win32": "winget install Gyan.FFmpeg", "darwin": "brew install ffmpeg"}


def find_ffmpeg(required: bool = True) -> str | None:
    """ffmpeg next to the executable, in ~/.mixgrab/bin, on PATH, or in the usual winget/Homebrew places."""
    import sys
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    home = os.environ.get("MIXGRAB_HOME", os.path.join(os.path.expanduser("~"), ".mixgrab"))
    local = [os.path.join(os.path.dirname(sys.executable), name), os.path.join(home, "bin", name)]
    for p in local:
        if getattr(sys, "frozen", False) or p.startswith(home):
            if os.path.isfile(p):
                return p
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    pats = [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
            "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    for p in pats:
        hits = glob.glob(p)
        if hits:
            return hits[0]
    if not required:
        return None
    raise SystemExit("ffmpeg not found. Install it: "
                     + FFMPEG_HINT.get(sys.platform, "your package manager, e.g. apt install ffmpeg"))


def download_audio(url: str, work_dir: str, progress=lambda done, total: None) -> str:
    os.makedirs(work_dir, exist_ok=True)
    existing = glob.glob(os.path.join(work_dir, "mixaudio.*"))
    if existing:
        return existing[0]
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": "bestaudio/best",
            "outtmpl": os.path.join(work_dir, "mixaudio.%(ext)s"), "noplaylist": True,
            "progress_hooks": [lambda d: progress(d.get("downloaded_bytes") or 0,
                                                  d.get("total_bytes") or d.get("total_bytes_estimate") or 0)]}
    from .tracklist import run_ydl
    return run_ydl(opts, lambda ydl: ydl.prepare_filename(ydl.extract_info(url, download=True)))


def clip(ffmpeg: str, src: str, at: int) -> bytes:
    r = subprocess.run([ffmpeg, "-v", "error", "-ss", str(at), "-t", str(CLIP), "-i", src,
                        "-ac", "1", "-ar", "16000", "-map_metadata", "-1", "-bitexact", "-f", "wav", "-"],
                       capture_output=True, check=True)
    return fix_wav_header(r.stdout)


def fix_wav_header(b: bytes) -> bytes:
    """ffmpeg can't seek back on a pipe, so a piped WAV has bogus RIFF/data sizes; Shazam's decoder
    then fails with 'unexpected end of file'. Patch the sizes in place."""
    if len(b) < 44 or b[:4] != b"RIFF":
        return b
    out = bytearray(b)
    out[4:8] = (len(b) - 8).to_bytes(4, "little")
    pos = 12
    while pos + 8 <= len(out):
        cid = bytes(out[pos:pos + 4])
        size = int.from_bytes(out[pos + 4:pos + 8], "little")
        if cid == b"data":
            out[pos + 4:pos + 8] = (len(out) - pos - 8).to_bytes(4, "little")
            break
        pos += 8 + size + (size & 1)
    return bytes(out)


# ---------------- optional fallback recognizers (used on clips Shazam can't place) ----------------
# SoundHound has no public recognition API (Houndify is enterprise-gated), so the keyed options are
# AudD (api_token, small free quota) and ACRCloud (host + access key/secret; best coverage for DJ sets).
async def _audd(session, token: str, data: bytes):
    import aiohttp
    form = aiohttp.FormData()
    form.add_field("api_token", token)
    form.add_field("file", data, filename="clip.wav", content_type="audio/wav")
    async with session.post("https://api.audd.io/", data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
        js = await r.json(content_type=None)
    if js.get("status") == "error":
        raise RuntimeError(f"AudD: {js.get('error', {}).get('error_message')}")
    res = js.get("result")
    return (res["artist"], res["title"]) if res and res.get("artist") and res.get("title") else None


async def _acrcloud(session, conf: dict, data: bytes):
    import aiohttp, base64, hashlib, hmac, time as _t
    ts = str(int(_t.time()))
    to_sign = "\n".join(["POST", "/v1/identify", conf["access_key"], "audio", "1", ts])
    sig = base64.b64encode(hmac.new(conf["access_secret"].encode(), to_sign.encode(), hashlib.sha1).digest()).decode()
    form = aiohttp.FormData()
    for k, v in {"access_key": conf["access_key"], "sample_bytes": str(len(data)), "timestamp": ts,
                 "signature": sig, "data_type": "audio", "signature_version": "1"}.items():
        form.add_field(k, v)
    form.add_field("sample", data, filename="clip.wav", content_type="audio/wav")
    url = f"https://{conf['host']}/v1/identify"
    async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as r:
        js = await r.json(content_type=None)
    code = js.get("status", {}).get("code")
    if code not in (0, 1001):                       # 1001 = no result
        raise RuntimeError(f"ACRCloud: {js.get('status', {}).get('msg')}")
    music = (js.get("metadata") or {}).get("music") or []
    if not music:
        return None
    m = music[0]
    artist = ", ".join(a.get("name", "") for a in m.get("artists", [])) or ""
    return (artist, m.get("title", "")) if artist and m.get("title") else None


def recognizers(conf: dict) -> list[tuple[str, object]]:
    """Fallback recognizers enabled by config: {'audd_token': ..., 'acrcloud': {host, access_key, access_secret}}."""
    out = []
    acr = conf.get("acrcloud") or {}
    if acr.get("host") and acr.get("access_key") and acr.get("access_secret"):
        out.append(("ACRCloud", lambda s, d: _acrcloud(s, acr, d)))
    if conf.get("audd_token"):
        out.append(("AudD", lambda s, d: _audd(s, conf["audd_token"], d)))
    return out


@dataclass
class Hit:
    at: int
    artist: str
    title: str

    @property
    def key(self) -> str:
        return norm(f"{self.artist} {self.title}")


async def scan(audio: str, duration: int, step: int, parallel: int = 3,
               progress=lambda done, total, matches: None, fallbacks: list | None = None) -> list[Hit]:
    """Fingerprint a clip every `step` seconds with Shazam; clips it misses go to the keyed fallbacks."""
    import aiohttp
    ffmpeg = find_ffmpeg()
    points = list(range(5, max(duration - CLIP, 6), step))
    log.debug("Identifying: %d clips of %ds, every %ds", len(points), CLIP, step)
    sem = asyncio.Semaphore(parallel)
    shazam = Shazam()
    hits: list[Hit] = []
    fallbacks = list(fallbacks or [])
    dead: set[str] = set()                      # fallbacks that errored hard (bad key, quota) are skipped
    done = 0

    async def shazam_one(data: bytes):
        for attempt in range(3):
            try:
                res = await shazam.recognize(data)
                break
            except Exception:  # rate limits / transient HTTP errors
                if attempt == 2:
                    raise
                await asyncio.sleep(5 * (attempt + 1))
        tr = res.get("track")
        return (tr.get("subtitle", "").strip(), tr.get("title", "").strip()) if tr else None

    async def one(session, at: int):
        nonlocal done
        async with sem:
            try:
                data = await asyncio.to_thread(clip, ffmpeg, audio, at)
                found = None
                try:
                    found = await shazam_one(data)
                except Exception as e:
                    log.debug("shazam at %ds failed: %s", at, e)
                for name, fn in fallbacks:
                    if found or name in dead:
                        continue
                    try:
                        found = await fn(session, data)
                    except Exception as e:
                        log.warning("%s disabled for this scan: %s", name, e)
                        dead.add(name)
                if found and found[0] and found[1]:
                    hits.append(Hit(at, found[0], found[1]))
            except Exception as e:
                log.debug("clip at %ds failed: %s", at, e)
            done += 1
            progress(done, len(points), len(hits))

    async with aiohttp.ClientSession() as session:
        await asyncio.gather(*(one(session, p) for p in points))
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
