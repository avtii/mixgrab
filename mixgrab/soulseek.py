"""Search Soulseek for each track, pick the best file, download it with fallbacks."""
from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Optional

from aioslsk.client import SoulSeekClient
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import (CredentialsSettings, ListeningSettings, NetworkSettings,
                              SearchReceiveSettings, SearchSettings, SharedDirectorySettingEntry,
                              Settings, SharesSettings)
from aioslsk.transfer.model import Transfer, TransferDirection
from aioslsk.transfer.state import TransferState

from .tracklist import Track

AUDIO_EXT = {"flac": 4.0, "wav": 3.0, "aiff": 3.0, "aif": 3.0, "alac": 3.5,
             "mp3": 0.0, "m4a": 1.5, "ogg": 1.5, "opus": 1.5}
LOSSLESS = {"flac", "wav", "aiff", "aif", "alac"}
VERSION_WORDS = {"remix", "rmx", "edit", "bootleg", "live", "cover", "karaoke", "instrumental",
                 "acapella", "dub", "vip", "rework", "reprise", "remaster", "mix", "version", "radio",
                 "extended", "ambient", "club", "sped", "slowed", "nightcore"}
STOP = {"the", "a", "an", "and", "of", "feat", "ft", "featuring", "vs", "x", "original", "mix"}

log = logging.getLogger("mixgrab")
# per-task status callback (web jobs set this; CLI uses Downloader.on_status)
STATUS_CB: contextvars.ContextVar = contextvars.ContextVar("mixgrab_status", default=None)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " ")
    s = re.sub(r"[^\w]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return [t for t in norm(s).split() if t not in STOP]


def strip_parens(s: str) -> str:
    return re.sub(r"\s*[\[(][^\])]*[\])]", "", s).strip()


def queries_for(t: Track) -> list[str]:
    artist = re.split(r"\s+(?:feat\.?|ft\.?|featuring|vs\.?|x)\s+", t.artist, flags=re.I)[0]
    title = re.sub(r"\((?:original mix|feat[^)]*|ft[^)]*)\)", "", t.title, flags=re.I)
    q = []
    for cand in (f"{artist} {title}", f"{artist} {strip_parens(title)}", strip_parens(title)):
        c = " ".join(w for w in norm(cand).split() if len(w) > 1 or w.isdigit())
        if c and c not in q and len(c.split()) >= 2:
            q.append(c)
    return q


@dataclass
class Candidate:
    username: str
    path: str
    size: int
    ext: str
    bitrate: Optional[int]
    duration: Optional[int]
    free_slot: bool
    queue: int
    speed: int
    score: float = 0.0

    def describe(self) -> str:
        br = f"{self.bitrate}kbps" if self.bitrate else "?"
        return f"{self.ext} {br} {self.size / 1e6:.1f}MB from {self.username} (score {self.score:.1f})"


def score(c: Candidate, t: Track, cfg) -> Optional[float]:
    parts = re.split(r"[\\/]", c.path)
    base = os.path.splitext(parts[-1])[0]
    base_tokens = set(norm(base).split())
    # only the file name and the folders right above it (album, artist) count: a share's root folders
    # ("Music", "Techno 2019 Collection") shouldn't make an unrelated file look like a match
    path_tokens = set(norm(" ".join(parts[-3:])).split())

    want_title = tokens(t.title) or norm(t.title).split()
    want_artist = tokens(t.artist) or norm(t.artist).split()
    if not want_title:                    # a title with no letters or digits ("???"): nothing to match on
        return None
    tf = sum(w in path_tokens for w in want_title) / len(want_title)
    af = sum(w in path_tokens for w in want_artist) / len(want_artist) if want_artist else 1
    # main title words (outside parentheses) must all be present
    core = tokens(strip_parens(t.title)) or want_title
    if not all(w in base_tokens or w in path_tokens for w in core):
        return None
    if tf < 0.75 or af < 0.5:
        return None

    s = 10 * tf + 5 * af
    # unexpected version words in the file name ("Remix", "Live", ...) that the tracklist doesn't ask for
    wanted = set(norm(t.title).split())
    extra = (base_tokens & VERSION_WORDS) - wanted
    s -= 4 * len(extra)

    # format / quality
    if c.ext in LOSSLESS:
        s += AUDIO_EXT[c.ext] if cfg["prefer_lossless"] else 1.0
    elif c.ext == "mp3":
        br = c.bitrate or (int(c.size * 8 / c.duration / 1000) if c.duration else None)
        if br is not None and br < cfg["min_bitrate"]:
            return None
        s += 3.5 if (br or 0) >= 310 else 2.5 if (br or 0) >= 250 else 1.0
    else:
        s += AUDIO_EXT.get(c.ext, 0)
    if c.ext in LOSSLESS and c.size > cfg["max_lossless_mb"] * 1e6:
        return None

    # duration sanity vs. the length the track has in the mix (mixes trim tracks, so be lenient)
    if c.duration and t.length:
        if c.duration < t.length * 0.5:
            return None
        if c.duration > t.length * 3 + 120:
            s -= 4
    if c.size < 1_000_000:
        return None

    # peer availability
    s += 3 if c.free_slot else 0
    s -= min(c.queue, 50) / 10
    s += min(c.speed / 1_000_000, 2)
    return s


def collect(results, t: Track, cfg) -> list[Candidate]:
    out, seen = [], set()
    for r in results:
        for f in r.shared_items:
            ext = (f.extension or os.path.splitext(f.filename)[1].lstrip(".")).lower()
            if not ext:
                ext = os.path.splitext(f.filename)[1].lstrip(".").lower()
            if ext not in AUDIO_EXT or (r.username, f.filename) in seen:
                continue
            seen.add((r.username, f.filename))
            attrs = {a.key: a.value for a in f.attributes}
            c = Candidate(r.username, f.filename, f.filesize, ext,
                          attrs.get(AttributeKey.BITRATE.value), attrs.get(AttributeKey.DURATION.value),
                          r.has_free_slots, r.queue_size, r.avg_speed)
            sc = score(c, t, cfg)
            if sc is not None:
                c.score = sc
                out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str) -> str:
    """A file/folder name that works on Windows, macOS and Linux. Never empty, never '.'/'..', never a
    reserved Windows device name; a long name is cut with a short hash of the whole, so two long names
    that start the same still differ."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip(" .")
    if not s:
        return "_"
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    if len(s) > 150:
        import hashlib
        s = s[:141].rstrip(" .") + "~" + hashlib.sha1(s.encode()).hexdigest()[:8]
    return s


def track_prefix(t: Track) -> str:
    return f"{t.index if t.file_no is None else t.file_no:02d} - {safe_name(t.label)}"


PARTIAL = (".part", ".tmp", ".ytdl")      # files still being written


class Downloader:
    def __init__(self, cfg: dict, share_dir: str):
        sl = cfg["soulseek"]
        self.cfg = cfg
        self.settings = Settings(
            credentials=CredentialsSettings(username=sl["username"], password=sl["password"]),
            network=NetworkSettings(listening=ListeningSettings(port=sl["listen_port"],
                                                                obfuscated_port=sl["listen_port"] + 1)),
            searches=SearchSettings(receive=SearchReceiveSettings(max_results=0)),
            shares=SharesSettings(
                download=share_dir,
                directories=[SharedDirectorySettingEntry(path=share_dir)] if sl.get("share_downloads", True) else [],
            ),
        )
        self.client = SoulSeekClient(self.settings)
        self.search_sem = asyncio.Semaphore(cfg["match"]["parallel_searches"])
        self.dl_sem = asyncio.Semaphore(cfg["match"]["parallel_downloads"])
        # UI hook: on_status(track_index, state, **info)
        self.on_status = lambda idx, state, **info: None

    def _status(self, idx, state, **info):
        cb = STATUS_CB.get() or self.on_status
        try:
            cb(idx, state, **info)
        except Exception:
            log.debug("status callback failed", exc_info=True)

    async def __aenter__(self):
        try:
            await self.client.start()
            await self.client.login()
        except BaseException:
            # __aexit__ never runs for a failed __aenter__: close the listening ports and connections here
            await _quietly(asyncio.shield(self.client.stop()))
            raise
        log.info("Logged in to Soulseek as %s", self.settings.credentials.username)
        return self

    async def __aexit__(self, *exc):
        await self.client.stop()

    async def search(self, t: Track) -> list[Candidate]:
        m = self.cfg["match"]
        cands: list[Candidate] = []
        for q in queries_for(t):
            self._status(t.index, "searching", query=q)
            async with self.search_sem:
                req = await self.client.searches.search(q)
                try:
                    await asyncio.sleep(m["search_seconds"])
                finally:                              # also when cancelled: stop collecting results
                    self.client.searches.remove_request(req)
            cands = collect(req.results, t, m)
            log.debug("[%02d] search %r -> %d responses, %d usable", t.index, q,
                     len(req.results), len(cands))
            if cands:
                break
        return cands

    async def fetch(self, t: Track, out_dir: str) -> tuple[Optional[str], str]:
        """Returns (final_path or None, message)."""
        prefix = track_prefix(t)
        for f in os.listdir(out_dir):
            if f.startswith(prefix + ".") and not f.endswith(PARTIAL) and ".yt." not in f:
                return os.path.join(out_dir, f), "already downloaded"

        cands = await self.search(t)
        if not cands:
            return None, "no matching files on Soulseek"

        m = self.cfg["match"]
        tried_users = set()
        attempts = 0
        for c in cands:
            if attempts >= m["max_attempts"]:
                break
            if c.username in tried_users:
                continue
            tried_users.add(c.username)
            attempts += 1
            final = os.path.join(out_dir, f"{prefix}.{c.ext}")
            part = final + f".{attempts}.part"
            log.debug("[%02d] try %d: %s | %s", t.index, attempts, c.describe(), c.path)
            self._status(t.index, "waiting", attempt=attempts, user=c.username, ext=c.ext,
                           total=c.size, candidates=len(cands))
            try:
                async with self.dl_sem:
                    ok, why = await self._download(c, part, m, t.index, attempts)
            except asyncio.CancelledError:
                try:
                    os.remove(part)
                except OSError:
                    pass
                raise
            if ok:
                os.replace(part, final)
                return final, c.describe()
            log.debug("[%02d] attempt %d failed: %s", t.index, attempts, why)
            self._status(t.index, "retry", reason=why, attempt=attempts)
            try:
                os.remove(part)
            except OSError:
                pass
        return None, f"all {attempts} download attempts failed"

    async def _download(self, c: Candidate, part: str, m: dict, idx: int = 0,
                        attempt: int = 1) -> tuple[bool, str]:
        tr = Transfer(c.username, c.path, TransferDirection.DOWNLOAD)
        tr.local_path = part
        started = last_change = time.monotonic()
        last_seen = None
        waiting = (TransferState.QUEUED, TransferState.VIRGIN, TransferState.INITIALIZING)
        try:
            try:
                tr = await asyncio.wait_for(self.client.transfers.add(tr), 30)
                await asyncio.wait_for(self.client.transfers.queue(tr), 30)
            except asyncio.TimeoutError:
                return False, "the Soulseek client did not answer"
            while True:
                await asyncio.sleep(1)
                st = tr.state.VALUE
                if st == TransferState.COMPLETE:
                    return True, "ok"
                if st in (TransferState.FAILED, TransferState.ABORTED, TransferState.INCOMPLETE):
                    return False, f"{st.name.lower()} ({tr.fail_reason or tr.abort_reason or 'peer refused'})"
                now = time.monotonic()
                if st == TransferState.DOWNLOADING or tr.bytes_transfered:
                    self._status(idx, "downloading", done=tr.bytes_transfered,
                                   total=tr.filesize or c.size, attempt=attempt, user=c.username)
                else:
                    self._status(idx, "queued", place=tr.place_in_queue, attempt=attempt,
                                   user=c.username, waited=int(now - started))
                if (st, tr.bytes_transfered) != last_seen:      # any progress or state change
                    last_seen, last_change = (st, tr.bytes_transfered), now
                if st in waiting and tr.bytes_transfered == 0:
                    if now - started > m["queue_timeout"]:
                        return False, f"stuck in remote queue for {m['queue_timeout']}s" + (
                            f" (place {tr.place_in_queue})" if tr.place_in_queue else "")
                elif now - last_change > m["stall_timeout"]:
                    # also covers a transfer that says 'downloading' but never sends a byte,
                    # and states the loop doesn't otherwise expect (paused, unset)
                    return False, f"stalled for {m['stall_timeout']}s"
        finally:
            # shielded: when the job is cancelled, the peer still hears that we stopped
            await _quietly(asyncio.shield(self._drop(tr)))

    async def _drop(self, tr):
        if tr.state.VALUE != TransferState.COMPLETE:
            await _quietly(self.client.transfers.abort(tr))
        await _quietly(self.client.transfers.remove(tr))


async def _quietly(aw, timeout: float = 15):
    """Await cleanup without letting its errors (or a hang) replace the real outcome."""
    try:
        await asyncio.wait_for(aw, timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
