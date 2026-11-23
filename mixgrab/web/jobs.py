"""Jobs: URL -> tracklist (+ audio ID) -> Soulseek downloads. State lives in memory and SQLite."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from typing import Optional

from .. import tracklist as tlmod
from ..engine import Engine, identify_into, identify_reason
from ..soulseek import STATUS_CB, Downloader, safe_name
from ..tracklist import Track
from . import settings as S

log = logging.getLogger("mixgrab.web")

ACTIVE_JOB = {"queued", "reading", "identifying", "downloading"}


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("pragma journal_mode=wal")
        self._touched: dict[str, float] = {}
        self.db.execute("create table if not exists jobs(id text primary key, created real, data text)")
        self.db.execute("create table if not exists files(id text primary key, job_id text, track int,"
                        " path text, size int, created real, last_access real)")
        self.db.commit()

    def save_job(self, job: dict):
        self.db.execute("insert or replace into jobs values(?,?,?)",
                        (job["id"], job["created"], json.dumps(job)))
        self.db.commit()

    def jobs(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("select data from jobs order by created desc")]

    def delete_job(self, job_id):
        self.db.execute("delete from jobs where id=?", (job_id,))
        self.db.execute("delete from files where job_id=?", (job_id,))
        self.db.commit()

    def add_file(self, fid, job_id, track, path, size):
        now = time.time()
        self.db.execute("insert or replace into files values(?,?,?,?,?,?,?)",
                        (fid, job_id, track, path, size, now, now))
        self.db.commit()

    def file(self, fid) -> Optional[dict]:
        r = self.db.execute("select id,job_id,track,path,size from files where id=?", (fid,)).fetchone()
        return dict(zip(("id", "job_id", "track", "path", "size"), r)) if r else None

    def touch(self, fid):
        # a player seeking sends many range requests: write at most once a minute per file
        now = time.time()
        if now - self._touched.get(fid, 0) < 60:
            return
        self._touched[fid] = now
        self.db.execute("update files set last_access=? where id=?", (now, fid))
        self.db.commit()

    def oldest_files(self):
        return self.db.execute("select id,job_id,track,path,size,last_access from files"
                               " order by last_access").fetchall()

    def total_size(self) -> int:
        return self.db.execute("select coalesce(sum(size),0) from files").fetchone()[0]

    def remove_file(self, fid):
        self.db.execute("delete from files where id=?", (fid,))
        self.db.commit()


_PATHS = re.compile(r"(?<![:\w/\\])(?:[A-Za-z]:)?(?:[\\/][\w.@~+-]+){2,}[\\/]?")


def friendly_error(e: Exception) -> str:
    """An error for the page: no file paths (they show where cookies and data live), no control characters."""
    if tlmod.is_bot_check(e):
        return ("YouTube blocked the server with a bot check. Sign in to YouTube under "
                "'YouTube cookies' in the sidebar, then press Resume.")
    s = str(e).replace("ERROR: ", "").strip() or type(e).__name__
    s = s.split(" Use --cookies")[0]
    s = _PATHS.sub("…", s)
    s = "".join(c if c.isprintable() else " " for c in s)
    return s[:300]


class Manager:
    def __init__(self):
        self.store = Store(S.DATA / "mixgrab.db")
        self.jobs: dict[str, dict] = {}
        for j in self.store.jobs():
            if j["status"] in ACTIVE_JOB:           # interrupted by a restart
                j["status"] = "interrupted"
                for t in j["tracks"]:
                    if t["state"] not in ("done", "missing", "expired"):
                        t["state"], t["note"] = "pending", ""
            self.jobs[j["id"]] = j
        for d in S.WORK.iterdir():                  # identify scratch of jobs that can't resume any more
            j = self.jobs.get(d.name)
            if not j or j["tracks"] or j["status"] != "interrupted":
                shutil.rmtree(d, ignore_errors=True)
        self.dl: Optional[Downloader] = None
        # the same engine the command-line tool runs on; one Soulseek login shared by all jobs
        self.engine = Engine({"soulseek": S.SLSK, "match": S.MATCH, "convert": S.CONVERT,
                              "youtube": S.YOUTUBE, "soundcloud": S.SOUNDCLOUD,
                              "identify": S.IDENTIFY})
        self.slsk_state = "offline"
        self.slsk_error = ""
        self._dirty: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[tuple[str, int]] = set()      # (job id, track index) being fetched right now
        self._zip_locks: dict[str, threading.Lock] = {}
        self._zip_guard = threading.Lock()
        tlmod.use_pot_provider(S.POT_URL)
        self.apply_cookies()

    def apply_cookies(self):
        """Point yt-dlp at the uploaded cookies.txt (or stop using it once removed)."""
        tlmod.use_cookies(str(S.COOKIES_FILE) if S.COOKIES_FILE.is_file() else None)

    def capture_browser_cookies(self) -> dict:
        """Take the signed-in YouTube cookies from the sign-in browser's profile (raises RuntimeError if not signed in)."""
        if not S.BROWSER_PROFILE:
            raise RuntimeError("the sign-in browser isn't set up on this server")
        tmp = S.COOKIES_FILE.with_suffix(".new")
        r = tlmod.export_youtube_cookies(tmp, "chromium", profile=S.BROWSER_PROFILE, keyring="BASICTEXT",
                                         require_signed_in=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, S.COOKIES_FILE)
        self.apply_cookies()
        return r

    async def _browser_cookie_loop(self):
        """YouTube rotates its cookies; while the sign-in browser stays signed in, keep our copy fresh."""
        while True:
            await asyncio.sleep(6 * 3600)
            try:
                await asyncio.to_thread(self.capture_browser_cookies)
                log.info("refreshed YouTube cookies from the sign-in browser")
            except Exception as e:
                log.info("sign-in browser: no fresh cookies (%s)", str(e).splitlines()[0][:120])

    # ---------- lifecycle ----------
    async def start(self):
        self._spawn(self._saver())
        self._spawn(self._cache_loop())
        if S.BROWSER_PROFILE:
            self._spawn(self._browser_cookie_loop())
        self._spawn(self._slsk_loop())

    async def stop(self):
        for t in list(self._tasks):
            t.cancel()
        self._flush()
        if self.dl:
            try:
                await self.dl.__aexit__(None, None, None)
            except Exception:
                pass

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def _slsk_loop(self):
        cfg = {"soulseek": S.SLSK, "match": S.MATCH}
        delay = 10
        while True:
            if self.dl is None:
                self.slsk_state = "connecting"
                try:
                    dl = Downloader(cfg, str(S.FILES))
                    await dl.__aenter__()
                    self.dl, self.slsk_state, self.slsk_error, delay = dl, "online", "", 10
                    self.engine.dl = dl
                except Exception as e:
                    self.slsk_state, self.slsk_error = "offline", str(e)[:200]
                    log.warning("soulseek login failed: %s", e)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 600)
                    continue
            else:
                sess = self.dl.client.session
                if sess is None:
                    self.slsk_state = "reconnecting"
                    try:
                        await self.dl.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self.dl = self.engine.dl = None
                    continue
            await asyncio.sleep(15)

    async def _wait_slsk(self, job, limit: int = 60):
        """Give Soulseek a minute to come online; after that the engine falls back to YouTube/SoundCloud."""
        waited = 0
        while (self.dl is None or self.slsk_state != "online") and waited < limit \
                and not (self.slsk_state == "offline" and self.slsk_error and waited):   # login failed: don't stall
            job["note"] = f"waiting for Soulseek ({self.slsk_state})"
            self.mark(job)
            await asyncio.sleep(3)
            waited += 3
        job["note"] = "" if self.slsk_state == "online" else "Soulseek offline: using YouTube/SoundCloud"

    # ---------- persistence ----------
    def mark(self, job):
        self._dirty.add(job["id"])

    def _flush(self):
        for jid in list(self._dirty):
            self._dirty.discard(jid)
            if jid in self.jobs:
                self.store.save_job(self.jobs[jid])

    async def _saver(self):
        while True:
            await asyncio.sleep(2)
            self._flush()

    # ---------- jobs ----------
    def create(self, url: str, identify: str) -> dict:
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "url": url, "identify": identify, "created": time.time(), "title": url,
               "found_in": "", "status": "queued", "note": "", "error": "", "tracks": [], "maybe": [],
               "progress": {}}
        self.jobs[jid] = job
        self.store.save_job(job)
        self._spawn(self._run(job))
        return job

    def summaries(self):
        out = []
        for j in sorted(self.jobs.values(), key=lambda j: -j["created"]):
            done = sum(t["state"] == "done" for t in j["tracks"])
            out.append({"id": j["id"], "title": j["title"], "status": j["status"], "created": j["created"],
                        "tracks": len(j["tracks"]), "done": done})
        return out

    def job_dir(self, job) -> str:
        d = S.FILES / job["id"]
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    async def _run(self, job):
        try:
            job["status"] = "reading"
            self.mark(job)
            tl = await asyncio.to_thread(tlmod.fetch_tracklist, job["url"], lambda *a: None)
            job["title"], job["found_in"] = tl.source_title, tl.found_in
            why = identify_reason(tl, job["identify"], S.IDENTIFY)
            if why:
                job["status"], job["note"] = "identifying", why
                self.mark(job)
                await self._identify(job, tl)
            job["tracks"] = [self._track_dict(t) for t in tl.tracks]
            job["note"] = ""
            if not job["tracks"]:
                job["status"], job["error"] = "error", "No tracks found in the video or its audio."
                self.mark(job)
                return
            await self._download_all(job)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("job %s failed", job["id"])
            job["status"], job["error"] = "error", friendly_error(e)
        finally:
            self.mark(job)

    @staticmethod
    def _track_dict(t: Track) -> dict:
        return {"index": t.index, "artist": t.artist, "title": t.title, "start": t.start,
                "length": t.length, "state": "pending", "note": "", "done": 0, "total": 0,
                "file": None, "ext": None, "size": None, "source": None, "origin": None, "mp3": None}

    async def _identify(self, job, tl):
        work = S.WORK / job["id"]
        prog = job["progress"]

        def on_audio(done, total):
            prog["audio"] = [done, total]
            self.mark(job)

        def on_clips(done, total, matches, wait=0):
            prog["clips"] = [done, total, matches, wait]
            self.mark(job)

        maybe, complete = await identify_into(tl, job["url"], str(work), S.IDENTIFY, on_audio, on_clips)
        job["found_in"] = tl.found_in
        job["maybe"] = [{"at": h.at, "artist": h.artist, "title": h.title} for h in maybe]
        # kept on failure, restart or an early stop, so a later run continues the scan instead of
        # starting over; the startup sweep removes it once the job can't resume any more
        if complete:
            shutil.rmtree(work, ignore_errors=True)
        else:
            log.warning("job %s: Shazam rate limit, identification stopped early", job["id"])

    async def _download_all(self, job, only: Optional[list[dict]] = None):
        job["status"] = "downloading"
        self.mark(job)
        await self._wait_slsk(job)
        tracks = only or [t for t in job["tracks"] if t["state"] != "done"]
        await asyncio.gather(*(self._fetch_track(job, t) for t in tracks))
        job["status"] = "done"
        self.mark(job)
        self._spawn(self._clean_cache())

    async def _fetch_track(self, job, t: dict):
        def status(idx, state, **i):
            t["state"] = state
            if state == "searching":
                t["note"] = f"“{i.get('query', '')}”"
            elif state == "waiting":
                t.update(note=f"{i.get('ext')} · peer {i.get('attempt')}", total=i.get("total") or 0, done=0)
            elif state == "queued":
                p = i.get("place")
                t["note"] = (f"queue place {p} · " if p else "in queue · ") + f"{i.get('waited', 0)}s"
            elif state == "downloading":
                t.update(done=i.get("done", 0), total=i.get("total") or t["total"],
                         note=f"peer {i.get('attempt')}")
            elif state == "retry":
                t.update(note=i.get("reason", "")[:80], done=0)
            elif state == "youtube":
                t.update(note="not on Soulseek, searching YouTube", done=0, total=0)
            elif state == "soundcloud":
                t.update(note="not on Soulseek or YouTube, searching SoundCloud", done=0, total=0)
            elif state == "converting":
                t["note"] = "making MP3 copy"
            self.mark(job)

        key = (job["id"], t["index"])
        if key in self._inflight:                 # already on its way: don't fetch it twice
            return
        self._inflight.add(key)
        STATUS_CB.set(status)
        tr = Track(t["index"], t["artist"], t["title"], t["start"], t["length"])
        try:
            try:
                r = await self.engine.fetch(tr, self.job_dir(job))
            except Exception as e:
                log.exception("track fetch failed")
                from ..engine import Fetched
                r = Fetched(None, None, "", f"error: {friendly_error(e)}")
        finally:
            self._inflight.discard(key)
        if job["id"] not in self.jobs:            # deleted while downloading: don't bring it back
            shutil.rmtree(S.FILES / job["id"], ignore_errors=True)
            return
        if r.path:
            fid = uuid.uuid4().hex
            size = os.path.getsize(r.path)
            self.store.add_file(fid, job["id"], t["index"], r.path, size)
            mp3_fid = None
            if r.mp3:
                mp3_fid = uuid.uuid4().hex
                self.store.add_file(mp3_fid, job["id"], t["index"], r.mp3, os.path.getsize(r.mp3))
            t.update(state="done", file=fid, ext=os.path.splitext(r.path)[1].lstrip("."), size=size,
                     done=size, total=size, note="", source=r.msg, origin=r.source, mp3=mp3_fid)
        else:
            t.update(state="missing", note=r.msg, done=0)
        self.mark(job)

    async def retry(self, job, index: int, artist: Optional[str], title: Optional[str]):
        t = next(t for t in job["tracks"] if t["index"] == index)
        for key in ("file", "mp3"):
            if t.get(key):
                self._delete_file(t[key])
        t["mp3"] = None
        if artist:
            t["artist"] = artist.strip()
        if title:
            t["title"] = title.strip()
        t.update(state="pending", note="", file=None, done=0, total=0)
        self._start_tracks(job, [t])

    def busy(self, job, index: int) -> bool:
        return (job["id"], index) in self._inflight

    def _start_tracks(self, job, tracks: list[dict]):
        self.mark(job)
        if job["status"] in ACTIVE_JOB:
            for t in tracks:
                self._spawn(self._fetch_track(job, t))
        else:
            job["status"] = "downloading"         # now, not when the task starts: delete/resume see it at once
            self._spawn(self._download_all(job, only=tracks))

    def resume(self, job):
        if job["status"] in ACTIVE_JOB:
            return
        job["error"] = ""
        job["status"] = "queued" if not job["tracks"] else "downloading"
        self.mark(job)
        if not job["tracks"]:
            self._spawn(self._run(job))
        else:
            self._spawn(self._download_all(job))

    async def add_track(self, job, artist: str, title: str):
        idx = max((t["index"] for t in job["tracks"]), default=0) + 1
        t = self._track_dict(Track(idx, artist.strip(), title.strip()))
        job["tracks"].append(t)
        self._start_tracks(job, [t])

    def delete(self, job):
        self.jobs.pop(job["id"], None)
        self.store.delete_job(job["id"])
        shutil.rmtree(S.FILES / job["id"], ignore_errors=True)
        shutil.rmtree(S.WORK / job["id"], ignore_errors=True)
        try:
            os.remove(S.ZIPS / f"{job['id']}.zip")
        except OSError:
            pass

    # ---------- zip ----------
    def build_zip(self, job) -> str:
        with self._zip_guard:
            lock = self._zip_locks.setdefault(job["id"], threading.Lock())
        with lock:                                # two clicks build it once; the second waits and reuses it
            return self._build_zip(job)

    def _build_zip(self, job) -> str:
        path = S.ZIPS / f"{job['id']}.zip"
        files = []
        for t in list(job["tracks"]):
            for key in ("file", "mp3"):
                f = self.store.file(t[key]) if t.get(key) else None
                if f and os.path.exists(f["path"]):
                    files.append(f["path"])
        newest = 0.0
        for p in files:
            try:
                newest = max(newest, os.path.getmtime(p))
            except OSError:
                pass
        if not path.exists() or path.stat().st_mtime < newest:
            fd, tmp = tempfile.mkstemp(dir=S.ZIPS, suffix=".tmp")
            os.close(fd)
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:   # audio is already compressed
                    for p in files:
                        try:
                            z.write(p, os.path.basename(p))
                        except FileNotFoundError:     # the cache cleaner got there first
                            pass
                    z.writestr("tracklist.txt", self.tracklist_text(job))
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        os.utime(path)
        return str(path)

    @staticmethod
    def tracklist_text(job) -> str:
        lines = [job["title"], job["url"], ""]
        lines += [f"{t['index']:02d}. {t['artist']} - {t['title']}" for t in job["tracks"]]
        return "\n".join(lines) + "\n"

    # ---------- cache ----------
    def _delete_file(self, fid):
        f = self.store.file(fid)
        if f:
            try:
                os.remove(f["path"])
            except OSError:
                pass
            self.store.remove_file(fid)

    def cache_usage(self) -> dict:
        files = self.store.total_size()
        zips = sum(p.stat().st_size for p in S.ZIPS.glob("*.zip"))
        du = shutil.disk_usage(S.DATA)
        return {"files": files, "zips": zips, "budget": int(S.CACHE_MAX_GB * 1e9),
                "disk_used": du.used, "disk_total": du.total}

    async def _cache_loop(self):
        while True:
            await asyncio.sleep(300)
            await self._clean_cache()

    async def _clean_cache(self):
        u = self.cache_usage()
        budget = u["budget"]
        used = u["files"] + u["zips"]
        over_budget = used >= S.CACHE_HIGH * budget
        over_disk = u["disk_used"] >= S.CACHE_HIGH * u["disk_total"]
        if not (over_budget or over_disk):
            return
        # zips are rebuildable: drop them first
        for p in S.ZIPS.glob("*.zip"):
            try:
                used -= p.stat().st_size
                p.unlink()
            except OSError:
                pass
        target = S.CACHE_FREE * (budget if over_budget else u["disk_total"])
        freed = 0
        now = time.time()
        for fid, job_id, track, path, size, last in self.store.oldest_files():
            if freed >= target:
                break
            job = self.jobs.get(job_id)
            if now - last < 600 or (job and job["status"] in ACTIVE_JOB):
                continue              # never pull a file from under a fresh download/stream or a running job
            self._delete_file(fid)
            freed += size or 0
            job = self.jobs.get(job_id)
            if job:
                for t in job["tracks"]:
                    if t.get("mp3") == fid:
                        t["mp3"] = None
                    if t.get("file") == fid:
                        if t.get("mp3"):
                            self._delete_file(t["mp3"])
                        t.update(state="expired", file=None, mp3=None, note="removed from cache", done=0)
                self.mark(job)
        log.info("cache cleanup freed %.1f MB", freed / 1e6)
