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


def _small_audio(ctx):
    """yt-dlp format selector: the smallest decent audio stream. Recognition works on 16 kHz mono, so a
    48 kbps stream identifies as well as a 160 kbps one and downloads about three times faster."""
    allf = [f for f in ctx.get("formats") or [] if "preview" not in (f.get("format_id") or "")
            and f.get("ext") != "mhtml"]
    fmts = [f for f in allf if f.get("acodec") not in (None, "none")] or allf
    fmts = [f for f in fmts if f.get("vcodec") in (None, "none")] or fmts
    rate = lambda f: f.get("abr") or f.get("tbr") or 0
    fmts = [f for f in fmts if rate(f) >= 40] or fmts
    if fmts:
        yield min(fmts, key=lambda f: rate(f) or 1e9)


def download_audio(url: str, work_dir: str, progress=lambda done, total: None) -> str:
    os.makedirs(work_dir, exist_ok=True)
    existing = [p for p in glob.glob(os.path.join(work_dir, "mixaudio.*"))
                if not p.endswith((".part", ".ytdl")) and ".part-Frag" not in p]
    if existing:
        return existing[0]
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": _small_audio,
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
    pos, data = 12, -1
    while pos + 8 <= len(out):
        cid = bytes(out[pos:pos + 4])
        size = int.from_bytes(out[pos + 4:pos + 8], "little")
        if cid == b"data":
            data = pos
            break
        pos += 8 + size + (size & 1)
    if data < 0:                      # a chunk size before it was bogus too: look for the tag instead
        data = out.find(b"data", 12)
    if 0 <= data <= len(out) - 8:
        out[data + 4:data + 8] = (len(out) - data - 8).to_bytes(4, "little")
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


SHAZAM_GAP = 1.0       # seconds between Shazam requests to start with; grows after each rate limit
REFINE_MIN_STEP = 60   # a first pass this coarse or coarser gets a second, in-between pass where needed
GIVE_UP_STRIKES = 5    # rate limits in a row (after 30+60+120+240 s of pauses) before the scan stops


class ShazamBusy(Exception):
    """Shazam keeps refusing this address; carry on later."""


class Pacer:
    """Spaces out requests to one service. When it says 'too many requests', every worker pauses
    (30 s, then 60, 120 ... up to 10 min) and requests stay further apart afterwards."""

    first_pause = 30

    def __init__(self, gap: float, on_wait=lambda secs: None):
        self.gap, self.on_wait = gap, on_wait
        self.next = 0.0            # loop time the next request may start
        self.paused_until = 0.0
        self.strikes = 0
        self.closed = False
        self.lock = asyncio.Lock()

    async def turn(self):
        async with self.lock:                   # one waiter at a time: the others queue behind it
            loop = asyncio.get_running_loop()
            shown = False
            # re-read self.next every second: a 429 elsewhere can push it out while we sleep
            while (left := self.next - loop.time()) > 0:
                if self.closed:
                    raise ShazamBusy()
                if loop.time() < self.paused_until:        # a rate-limit pause, not normal spacing
                    shown = True
                    self.on_wait(int(left + 0.999))
                await asyncio.sleep(min(1.0, left))
            if shown:
                self.on_wait(0)
            if self.closed:
                raise ShazamBusy()
            self.next = loop.time() + self.gap

    def limited(self) -> float:
        now = asyncio.get_running_loop().time()
        if now < self.paused_until:             # a request sent before the pause began: same strike
            return self.paused_until - now
        self.strikes += 1
        pause = min(self.first_pause * 2 ** (self.strikes - 1), 600)
        self.gap = min(self.gap * 1.5, 8.0)
        self.paused_until = self.next = max(self.next, now + pause)
        return pause

    def ok(self):
        self.strikes = 0


def _shazam() -> Shazam:
    """A Shazam client that reports a rate limit at once. shazamio's default quietly retries a 429
    up to 20 times with growing waits, which looks like a frozen progress bar for minutes."""
    try:
        from aiohttp_retry import ExponentialRetry
        from shazamio.client import HTTPClient
        return Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(attempts=1, statuses=set())))
    except (ImportError, TypeError):
        return Shazam()


def _is_rate_limit(e: Exception) -> bool:
    try:
        from shazamio.exceptions import RateLimited
        if isinstance(e, RateLimited):
            return True
    except ImportError:
        pass
    return "429" in str(e) or "too many requests" in str(e).lower()


def _load_cache(path: str | None) -> dict[int, tuple[str, str] | None]:
    """Per-clip answers saved by an earlier, interrupted run of the same mix."""
    import json
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():          # keep only well-formed entries; anything else gets asked again
        try:
            at = int(k)
        except ValueError:
            continue
        if v is None:
            out[at] = None
        elif isinstance(v, list) and len(v) == 2 and all(isinstance(x, str) for x in v):
            out[at] = (v[0], v[1])
    return out


def _save_cache(path: str | None, cache: dict):
    import json
    if not path:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({str(k): v for k, v in sorted(cache.items())}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("could not save %s: %s", path, e)


def listed_spans(tracks: list[Track], hold: int = 180, margin: int = 15) -> list[tuple[int, int]]:
    """Stretches of the mix a timestamped tracklist already names: from each track's start for up to
    `hold` seconds (or until the next listed track), less a margin for the blend at either end."""
    timed = sorted((t.start for t in tracks if t.start is not None))
    spans = []
    for i, s in enumerate(timed):
        end = min(s + hold, timed[i + 1] if i + 1 < len(timed) else s + hold)
        if end - margin > s + margin:
            spans.append((s + margin, end - margin))
    return spans


async def scan(audio: str, duration: int, step: int, parallel: int = 3,
               progress=lambda done, total, matches, wait=0: None, fallbacks: list | None = None,
               cache: str | None = None, skip: list[tuple[int, int]] | None = None,
               refine: bool = True) -> tuple[list[Hit], bool]:
    """Fingerprint a clip every `step` seconds with Shazam, then (for a coarse step) a clip half-way
    between neighbours that disagree or both missed, which is where one song turns into the next.
    Stretches in `skip` (already in the tracklist) aren't sampled. Clips Shazam misses go to the
    keyed fallbacks. Every answer is saved to `cache`, so an interrupted scan picks up where it was.
    progress(done, total, matches, wait) gets wait > 0 while Shazam is rate-limiting us.
    Returns (hits, complete); complete is False when Shazam kept refusing and the scan stopped early."""
    import aiohttp
    ffmpeg = find_ffmpeg()
    skip = list(skip or [])
    saved = _load_cache(cache)
    coarse = list(range(5, max(duration - CLIP, 6), step))
    sem = asyncio.Semaphore(parallel)
    shazam = _shazam()
    hits: dict[int, Hit] = {}
    seen: dict[int, object] = {}               # at -> song key, None (no match) or a listed span
    fallbacks = list(fallbacks or [])
    dead: set[str] = set()                     # fallbacks that errored hard (bad key, quota) are skipped
    st = {"done": 0, "total": 0, "wait": 0}

    def report():
        progress(st["done"], st["total"], len(hits), st["wait"])

    def on_wait(secs: int):
        st["wait"] = secs
        report()

    pacer = Pacer(SHAZAM_GAP, on_wait)

    def span_of(at: int):
        return next((("listed", a) for a, b in skip if a <= at < b), None)

    async def ask_shazam(data: bytes):
        errors = 0
        while True:
            await pacer.turn()
            try:
                res = await asyncio.wait_for(shazam.recognize(data), 30)
            except Exception as e:
                if _is_rate_limit(e):
                    pause = pacer.limited()
                    if pacer.strikes >= GIVE_UP_STRIKES:
                        pacer.closed = True
                        on_wait(0)
                        raise ShazamBusy() from None
                    log.info("Shazam is rate-limiting; pausing %.0fs", pause)
                else:
                    errors += 1
                    if errors >= 3:
                        raise
                continue
            pacer.ok()
            tr = res.get("track")
            return (tr.get("subtitle", "").strip(), tr.get("title", "").strip()) if tr else None

    def record(at: int, found):
        if found and found[0] and found[1]:
            hits[at] = Hit(at, found[0], found[1])
            seen[at] = hits[at].key
        else:
            seen[at] = None

    async def one(session, at: int):
        if at in saved:
            record(at, saved[at])
        else:
            async with sem:
                try:
                    data = await asyncio.to_thread(clip, ffmpeg, audio, at)
                    found, answered = None, False
                    try:
                        found, answered = await ask_shazam(data), True
                    except ShazamBusy:
                        pass
                    except Exception as e:
                        log.debug("shazam at %ds failed: %s", at, e)
                    for name, fn in fallbacks:
                        if found or name in dead:
                            continue
                        try:
                            found, answered = await fn(session, data), True
                        except Exception as e:
                            log.warning("%s disabled for this scan: %s", name, e)
                            dead.add(name)
                    if answered:                # errors aren't saved: a later run asks again
                        saved[at] = found
                        _save_cache(cache, saved)
                    record(at, found)
                except Exception as e:
                    log.debug("clip at %ds failed: %s", at, e)
        st["done"] += 1
        report()

    async def run(points: list[int]):
        st["total"] += len(points)
        report()
        await asyncio.gather(*(one(session, p) for p in points))

    async with aiohttp.ClientSession() as session:
        first = []
        for p in coarse:
            if span_of(p):
                seen[p] = span_of(p)
            else:
                first.append(p)
        log.debug("Identifying: %d clips of %ds, every %ds (%d skipped as listed)",
                  len(first), CLIP, step, len(coarse) - len(first))
        await run(first)
        if refine and step >= REFINE_MIN_STEP and not pacer.closed:
            marks = sorted(seen)
            mids = []
            for a, b in zip(marks, marks[1:]):
                if seen[a] != seen[b] or seen[a] is None:
                    m = (a + b) // 2
                    if not span_of(m):
                        mids.append(m)
            log.debug("Second pass: %d clips where neighbours differ or missed", len(mids))
            await run(mids)
    close = getattr(getattr(shazam, "http_client", None), "close", None)
    if close:
        try:
            await close()
        except Exception:
            pass
    if pacer.closed:
        log.warning("Shazam kept rate-limiting; stopped after %d of %d clips", st["done"], st["total"])
    return sorted(hits.values(), key=lambda h: h.at), not pacer.closed


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
