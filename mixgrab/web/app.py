"""mixgrab web: a private, single-owner web UI over the mixgrab pipeline."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import tempfile
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import tracklist as tlmod
from . import settings as S
from .jobs import ACTIVE_JOB, Manager

log = logging.getLogger("mixgrab.web")
STATIC = Path(__file__).parent / "static"
COOKIE = "mg_session"
mgr: Manager


@asynccontextmanager
async def lifespan(app):
    global mgr
    if not S.OWNER_PASSWORD or len(S.OWNER_PASSWORD) < 12:
        raise SystemExit("Set MIXGRAB_PASSWORD (at least 12 characters).")
    mgr = Manager()
    await mgr.start()
    yield
    await mgr.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


# ---------------- signing ----------------
def sign(msg: str) -> str:
    mac = hmac.new(S.SECRET, msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode().rstrip("=")


def make_session() -> str:
    exp = int(time.time()) + S.SESSION_DAYS * 86400
    body = f"{exp}.{secrets.token_hex(8)}"
    return f"{body}.{sign('s:' + body)}"


def session_ok(tok: Optional[str]) -> bool:
    if not tok or tok.count(".") != 2:
        return False
    exp, nonce, sig = tok.split(".")
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign(f"s:{exp}.{nonce}"))


def link(kind: str, ident: str) -> str:
    exp = int(time.time()) + S.LINK_TTL_MIN * 60
    return f"/{kind}/{ident}?exp={exp}&sig={sign(f'{kind}:{ident}:{exp}')}"


def link_ok(kind: str, ident: str, exp: str, sig: str) -> bool:
    return exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, sign(f"{kind}:{ident}:{exp}"))


def require_owner(request: Request):
    if not session_ok(request.cookies.get(COOKIE)):
        raise HTTPException(401, "login required")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data: https://i.ytimg.com; media-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
    return resp


# ---------------- auth ----------------
FAILS: dict[str, deque] = defaultdict(deque)
GLOBAL_FAILS: deque = deque()


def client_ip(request: Request) -> str:
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


class Login(BaseModel):
    username: str
    password: str


@app.post("/api/login")
async def login(body: Login, request: Request, response: Response):
    ip, now = client_ip(request), time.time()
    q = FAILS[ip]
    while q and now - q[0] > 900:
        q.popleft()
    while GLOBAL_FAILS and now - GLOBAL_FAILS[0] > 900:
        GLOBAL_FAILS.popleft()
    if len(q) >= 5 or len(GLOBAL_FAILS) >= 30:
        raise HTTPException(429, "Too many attempts. Try again in 15 minutes.")
    ok = hmac.compare_digest(body.username.strip().lower(), S.OWNER_USER.lower()) & \
        hmac.compare_digest(body.password.encode(), S.OWNER_PASSWORD.encode())
    if not ok:
        q.append(now)
        GLOBAL_FAILS.append(now)
        await _sleep(1.0)
        raise HTTPException(401, "Wrong username or password.")
    response.set_cookie(COOKIE, make_session(), max_age=S.SESSION_DAYS * 86400, httponly=True,
                        secure=S.COOKIE_SECURE, samesite="strict", path="/")
    return {"ok": True}


async def _sleep(s):
    import asyncio
    await asyncio.sleep(s)


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_owner)])
async def me():
    u = mgr.cache_usage()
    return {"user": S.OWNER_USER, "soulseek": {"state": mgr.slsk_state, "error": mgr.slsk_error,
                                               "username": S.SLSK["username"]},
            "cache": u, "link_ttl_min": S.LINK_TTL_MIN,
            "youtube_cookies": (S.COOKIES_FILE.stat().st_mtime if S.COOKIES_FILE.is_file() else None),
            "youtube_login": S.LOGIN_PATH if S.BROWSER_PROFILE else None}


@app.get("/api/authcheck")
async def authcheck(request: Request):
    """For Caddy forward_auth: only the signed-in owner may reach the YouTube sign-in browser."""
    if not session_ok(request.cookies.get(COOKIE)):
        raise HTTPException(401, "login required")
    return Response(status_code=204)


@app.post("/api/youtube-login/capture", dependencies=[Depends(require_owner)])
async def capture_login():
    """Take the cookies once the owner has signed in. Chromium writes new cookies to disk within ~30 s, so retry."""
    last = ""
    for _ in range(10):
        try:
            r = await asyncio.to_thread(mgr.capture_browser_cookies)
            return {"ok": True, "cookies": r["cookies"]}
        except RuntimeError as e:
            last = str(e)
        await asyncio.sleep(5)
    log.info("YouTube sign-in capture failed: %s", last.replace("\n", "; ")[:200])
    raise HTTPException(400, "Not signed in to YouTube yet. Finish signing in in the sign-in window "
                             "(you should see your avatar on youtube.com), then try again.")


# ---------------- YouTube cookies ----------------
@app.post("/api/youtube-cookies", dependencies=[Depends(require_owner)])
async def upload_cookies(request: Request):
    raw = await request.body()
    if raw.startswith(b"SQLite format 3\x00"):     # Firefox's own cookies.sqlite, uploaded as is
        return await asyncio.to_thread(_cookies_from_firefox_db, raw)
    if len(raw) > 1_000_000:
        raise HTTPException(413, "That file is too big for a cookies.txt.")
    text = raw.decode("utf-8", "replace")
    rows = [l.split("\t") for l in text.splitlines()
            if l.strip() and (not l.startswith("#") or l.startswith("#HttpOnly_"))]
    yt = [r for r in rows if len(r) == 7 and ("youtube.com" in r[0] or "google.com" in r[0])]
    if not yt:
        raise HTTPException(400, "No YouTube cookies found. Export cookies.txt (Netscape format) "
                                 "from a browser tab where you are signed in to youtube.com.")
    if not text.startswith("# Netscape HTTP Cookie File"):
        text = "# Netscape HTTP Cookie File\n" + text
    S.COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = S.COOKIES_FILE.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, S.COOKIES_FILE)
    mgr.apply_cookies()
    return {"ok": True, "cookies": len(yt)}


def _cookies_from_firefox_db(raw: bytes) -> dict:
    """Keep only the YouTube/Google cookies from an uploaded Firefox cookies.sqlite; the database itself is discarded."""
    if len(raw) > 64_000_000:
        raise HTTPException(413, "That cookies.sqlite is too big.")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "cookies.sqlite").write_bytes(raw)
        try:
            r = tlmod.export_youtube_cookies(S.COOKIES_FILE, "firefox", profile=d)
        except RuntimeError:
            raise HTTPException(400, "No YouTube cookies in that file. Sign in to youtube.com in Firefox, "
                                     "close Firefox, then upload cookies.sqlite again.")
    os.chmod(S.COOKIES_FILE, 0o600)
    mgr.apply_cookies()
    if not r["signed_in"]:
        return {"ok": True, "cookies": r["cookies"],
                "warning": "Firefox isn't signed in to YouTube, so these cookies may not get past the bot check."}
    return {"ok": True, "cookies": r["cookies"]}


@app.delete("/api/youtube-cookies", dependencies=[Depends(require_owner)])
async def delete_cookies():
    S.COOKIES_FILE.unlink(missing_ok=True)
    mgr.apply_cookies()
    return {"ok": True}


# ---------------- jobs ----------------
URL_RE = re.compile(r"^https?://((www\.|m\.|music\.)?(youtube\.com|youtu\.be)|(www\.|m\.|on\.)?soundcloud\.com"
                    r"|(www\.|m\.)?mixcloud\.com)/", re.I)


class NewJob(BaseModel):
    url: str
    identify: str = "auto"


def public_job(job: dict) -> dict:
    j = dict(job)
    tracks = []
    for t in job["tracks"]:
        t = dict(t)
        if t.get("file"):
            t["url"] = link("f", t["file"])
        if t.get("mp3"):
            t["mp3_url"] = link("f", t["mp3"])
        tracks.append(t)
    j["tracks"] = tracks
    if any(t.get("file") for t in job["tracks"]):
        j["zip_url"] = link("zip", job["id"])
    return j


def get_job(job_id: str) -> dict:
    job = mgr.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return job


@app.post("/api/jobs", dependencies=[Depends(require_owner)])
async def create_job(body: NewJob):
    url = body.url.strip()
    if not URL_RE.match(url):
        raise HTTPException(400, "Paste a YouTube, SoundCloud or Mixcloud link.")
    if body.identify not in ("auto", "always", "never"):
        raise HTTPException(400, "bad identify mode")
    return public_job(mgr.create(url, body.identify))


@app.get("/api/jobs", dependencies=[Depends(require_owner)])
async def list_jobs():
    return mgr.summaries()


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_owner)])
async def job_detail(job_id: str):
    return public_job(get_job(job_id))


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_owner)])
async def delete_job(job_id: str):
    job = get_job(job_id)
    if job["status"] in ACTIVE_JOB:
        raise HTTPException(409, "Wait until this search finishes.")
    mgr.delete(job)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/resume", dependencies=[Depends(require_owner)])
async def resume_job(job_id: str):
    mgr.resume(get_job(job_id))
    return {"ok": True}


class TrackEdit(BaseModel):
    artist: Optional[str] = None
    title: Optional[str] = None


@app.post("/api/jobs/{job_id}/tracks/{index}/retry", dependencies=[Depends(require_owner)])
async def retry_track(job_id: str, index: int, body: TrackEdit):
    job = get_job(job_id)
    if not any(t["index"] == index for t in job["tracks"]):
        raise HTTPException(404, "no such track")
    await mgr.retry(job, index, body.artist, body.title)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/tracks", dependencies=[Depends(require_owner)])
async def add_track(job_id: str, body: TrackEdit):
    if not (body.artist and body.title):
        raise HTTPException(400, "artist and title are required")
    await mgr.add_track(get_job(job_id), body.artist, body.title)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/tracklist.txt", dependencies=[Depends(require_owner)])
async def tracklist_txt(job_id: str):
    return Response(mgr.tracklist_text(get_job(job_id)), media_type="text/plain; charset=utf-8")


# ---------------- files (session + expiring signed link) ----------------
@app.get("/f/{fid}", dependencies=[Depends(require_owner)])
async def file_dl(fid: str, exp: str = "", sig: str = "", inline: int = 0):
    if not link_ok("f", fid, exp, sig):
        raise HTTPException(410, "This link has expired. Reload the page for a fresh one.")
    f = mgr.store.file(fid)
    if not f or not os.path.exists(f["path"]):
        raise HTTPException(404, "File is no longer in the cache.")
    mgr.store.touch(fid)
    name = os.path.basename(f["path"])
    return FileResponse(f["path"], filename=name,
                        content_disposition_type="inline" if inline else "attachment")


@app.get("/zip/{job_id}", dependencies=[Depends(require_owner)])
async def zip_dl(job_id: str, exp: str = "", sig: str = ""):
    if not link_ok("zip", job_id, exp, sig):
        raise HTTPException(410, "This link has expired. Reload the page for a fresh one.")
    job = get_job(job_id)
    import asyncio
    path = await asyncio.to_thread(mgr.build_zip, job)
    for t in job["tracks"]:
        if t.get("file"):
            mgr.store.touch(t["file"])
            if t.get("mp3"):
                mgr.store.touch(t["mp3"])
    from ..soulseek import safe_name
    return FileResponse(path, filename=f"{safe_name(job['title'])}.zip", media_type="application/zip")


# ---------------- page ----------------
@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/robots.txt")
async def robots():
    return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("aioslsk", "shazamio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="info")


if __name__ == "__main__":
    main()
