"""Server settings from environment variables (see deploy/.env.example)."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def _env(name, default=None, cast=str):
    v = os.environ.get(name)
    return default if v in (None, "") else cast(v)


DATA = Path(_env("MIXGRAB_DATA", "./data")).resolve()
FILES = DATA / "files"
ZIPS = DATA / "zips"
WORK = DATA / "work"
for d in (DATA, FILES, ZIPS, WORK):
    d.mkdir(parents=True, exist_ok=True)


def _persisted(name: str, make):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text())
    v = make()
    p.write_text(json.dumps(v))
    os.chmod(p, 0o600)
    return v


# Single owner. No registration exists anywhere in the app.
OWNER_USER = _env("MIXGRAB_USER", "owner")
OWNER_PASSWORD = _env("MIXGRAB_PASSWORD", "")
SECRET = (_env("MIXGRAB_SECRET") or _persisted("secret.json", lambda: secrets.token_hex(32))).encode()
SESSION_DAYS = _env("SESSION_DAYS", 30, int)
COOKIE_SECURE = _env("COOKIE_SECURE", "1") == "1"

_slsk = _persisted("slsk.json", lambda: {"username": f"mixgrab_{secrets.token_hex(3)}",
                                          "password": secrets.token_urlsafe(14)})
SLSK = {
    "username": _env("SLSK_USERNAME", _slsk["username"]),
    "password": _env("SLSK_PASSWORD", _slsk["password"]),
    "listen_port": _env("SLSK_PORT", 61234, int),
    "share_downloads": _env("SLSK_SHARE", "1") == "1",
}

CACHE_MAX_GB = _env("CACHE_MAX_GB", 15.0, float)      # cache budget
CACHE_HIGH = _env("CACHE_HIGH", 0.92, float)          # start cleaning at 92% of budget (or disk)
CACHE_FREE = _env("CACHE_FREE", 0.12, float)          # then delete the oldest 12%
LINK_TTL_MIN = _env("LINK_TTL_MIN", 60 * 24, int)     # signed file links expire after this
YTDLP_COOKIES = _env("YTDLP_COOKIES")                 # optional cookies.txt for YouTube

MATCH = {
    "prefer_lossless": _env("PREFER_LOSSLESS", "1") == "1",
    "min_bitrate": 192, "max_lossless_mb": 250, "search_seconds": 12,
    "parallel_searches": 2, "parallel_downloads": 4, "max_attempts": 6,
    "queue_timeout": 120, "stall_timeout": 60,
}
CONVERT = {"mp3": _env("CONVERT_MP3", "1") == "1", "bitrate": _env("MP3_BITRATE", 320, int)}
YOUTUBE = {"fallback": _env("YOUTUBE_FALLBACK", "1") == "1", "quality": _env("YOUTUBE_QUALITY", "0")}
IDENTIFY = {"auto": True, "step": 45, "min_hits": 2, "max_avg_track_len": 600,
            "audd_token": _env("AUDD_TOKEN"),
            "acrcloud": {"host": _env("ACR_HOST"), "access_key": _env("ACR_ACCESS_KEY"),
                         "access_secret": _env("ACR_ACCESS_SECRET")}}
