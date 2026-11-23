"""Look a mix / album up in public tracklist databases when YouTube itself has no tracklist.

  * MixesDB (MediaWiki API) - DJ sets, radio shows, festival recordings
  * MusicBrainz            - albums / EPs (track titles + lengths)
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Optional

from .tracklist import (SEP_RE, TRAIL_JUNK_RE, Track, Tracklist, VIDEO_JUNK_RE, _fill_lengths,
                        album_artist, parse_line)

log = logging.getLogger("mixgrab")
UA = "mixgrab/0.1 (+https://github.com/avtii/mixgrab)"
STOP = {"the", "a", "an", "of", "and", "at", "in", "on", "live", "set", "dj", "mix", "full", "album",
        "official", "video", "audio", "hd", "4k", "presents", "pres", "b2b", "x", "vs", "with", "by"}


def _get(url: str, timeout: int = 15) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _words(s: str) -> set[str]:
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return {w for w in s.split() if w not in STOP and (len(w) > 1 or w.isdigit())}


def _overlap(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa) if wa else 0.0


# ---------------- MixesDB ----------------
MDB = "https://www.mixesdb.com/w/api.php"
MDB_LINE = re.compile(r"^\s*(?:#\s*)?(?:\[(\d{1,3}(?::\d{2}){0,2})\]\s*)?(.*)$")
LABEL_RE = re.compile(r"\s*\[[^\]]*\]\s*$")


def _mdb_search(q: str, limit: int = 8) -> list[str]:
    url = MDB + "?" + urllib.parse.urlencode(
        {"action": "query", "list": "search", "srsearch": q, "srlimit": limit, "format": "json"})
    return [h["title"] for h in _get(url).get("query", {}).get("search", [])]


def _mdb_wikitext(page: str) -> str:
    url = MDB + "?" + urllib.parse.urlencode(
        {"action": "parse", "page": page, "prop": "wikitext", "format": "json", "formatversion": 2})
    return _get(url).get("parse", {}).get("wikitext", "")


def parse_mixesdb(wikitext: str) -> tuple[list[Track], int]:
    """'[04] Roku - Acid Amnesia [Hardgroove]' lines inside <list>; '[06] ?' is an unknown track."""
    m = re.search(r"<list>(.*?)</list>", wikitext, re.S) or \
        re.search(r"==\s*Tracklist\s*==(.*?)(?:\n==[^=]|\[\[Category|$)", wikitext, re.S)
    if not m:
        return [], 0
    tracks, unknown = [], 0
    for raw in m.group(1).splitlines():
        lm = MDB_LINE.match(raw.strip())
        if not lm or not lm.group(2).strip() or lm.group(2).strip() in ("...", "…"):
            continue
        ts, body = lm.group(1), lm.group(2).strip()
        body = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", body)          # [[link|text]]
        body = re.sub(r"'''?|<[^>]+>|\{\{[^}]*\}\}", "", body).strip()
        if body.strip("? ") == "" or re.fullmatch(r"(?i)(id|unknown)(\s*-\s*id)?", body):
            unknown += 1
            continue
        body = LABEL_RE.sub("", body)
        p = parse_line(body)
        if not p:
            continue
        start = None
        if ts:
            parts = [int(x) for x in ts.split(":")]
            start = parts[0] * 60 if len(parts) == 1 else (
                parts[0] * 60 + parts[1] if len(parts) == 2 else parts[0] * 3600 + parts[1] * 60 + parts[2])
        tracks.append(Track(len(tracks) + 1, p[0], p[1], start))
    # minute stamps must increase, otherwise drop them
    st = [t.start for t in tracks if t.start is not None]
    if st != sorted(st):
        for t in tracks:
            t.start = None
    return tracks, unknown


def mixesdb(info: dict) -> Optional[Tracklist]:
    vid = info.get("id") or ""
    title = info.get("title") or ""
    clean = VIDEO_JUNK_RE.sub("", title)
    clean = re.sub(r"[|@•·:/\\()\[\]\"]", " ", clean)
    ordered = list(dict.fromkeys(w for w in re.sub(r"[^\w\s]", " ", clean.lower()).split()
                                 if w in _words(w)))           # keep title word order, drop stop words
    queries = [vid] if vid else []                               # search is AND-of-terms: shrink step by step
    queries += [" ".join(ordered[:n]) for n in (len(ordered), 4, 3, 2) if 2 <= n <= len(ordered)]
    queries = list(dict.fromkeys(q for q in queries if q.strip()))
    seen, best, best_score = set(), None, 0.0
    for q in queries:
        try:
            pages = _mdb_search(q)
        except Exception as e:
            log.debug("mixesdb search failed: %s", e)
            return None
        for page in pages:
            if page in seen:
                continue
            seen.add(page)
            score = _overlap(page, clean)
            if score < 0.4 and q != vid:
                continue
            try:
                wt = _mdb_wikitext(page)
            except Exception as e:
                log.debug("mixesdb page %s failed: %s", page, e)
                continue
            if vid and vid in wt:
                score += 2.0                              # page embeds this exact video
            tracks, unknown = parse_mixesdb(wt)
            if len(tracks) < 2:
                continue
            if score > best_score:
                best, best_score = (page, tracks, unknown, vid and vid in wt), score
            time.sleep(0.3)
        if best and best_score >= 2.0:
            break
    if not best or best_score < 0.5:
        return None
    page, tracks, unknown, exact = best
    total = info.get("duration")
    _fill_lengths(tracks, total)
    tl = Tracklist(title, f"MixesDB: {page}", tracks, total, unknown)
    tl.verified = bool(exact)                             # the MixesDB page embeds this very video
    return tl


# ---------------- MusicBrainz ----------------
MB = "https://musicbrainz.org/ws/2/"


def _album_name(info: dict, artist: str) -> str:
    t = VIDEO_JUNK_RE.sub("", info.get("title") or "")
    parts = [p.strip() for p in SEP_RE.split(t, maxsplit=1)]
    name = parts[1] if len(parts) == 2 and parts[0].lower() == artist.lower() else t
    name = re.sub(r"\(\s*\d{4}\s*\)|\[\s*\d{4}\s*\]|\b(full album|album|ep|lp)\b", " ", name, flags=re.I)
    return TRAIL_JUNK_RE.sub("", re.sub(r"\s+", " ", name)).strip()


def musicbrainz(info: dict) -> Optional[Tracklist]:
    artist = album_artist(info)
    if not artist:
        return None
    album = _album_name(info, artist)
    if not album:
        return None
    q = f'release:"{album}" AND artist:"{artist}"'
    try:
        res = _get(MB + "release?" + urllib.parse.urlencode({"query": q, "fmt": "json", "limit": 5}))
    except Exception as e:
        log.debug("musicbrainz search failed: %s", e)
        return None
    dur = info.get("duration") or 0
    best = None
    for rel in res.get("releases", []):
        if rel.get("score", 0) < 85:
            continue
        time.sleep(1.1)                                   # MusicBrainz: 1 request / second
        try:
            full = _get(MB + f"release/{rel['id']}?" + urllib.parse.urlencode(
                {"inc": "recordings+artist-credits", "fmt": "json"}))
        except Exception as e:
            log.debug("musicbrainz release failed: %s", e)
            continue
        tracks = []
        for medium in full.get("media", []):
            for tr in medium.get("tracks", []):
                credit = "".join(c.get("name", "") + c.get("joinphrase", "")
                                 for c in tr.get("artist-credit", [])) or artist
                length = (tr.get("length") or 0) // 1000 or None
                tracks.append(Track(len(tracks) + 1, credit, tr.get("title", ""), None, length))
        if len(tracks) < 2:
            continue
        total = sum(t.length or 0 for t in tracks)
        fit = abs(total - dur) / dur if dur and total else 1.0
        if best is None or fit < best[0]:
            best = (fit, rel, tracks)
    if not best or (dur and best[0] > 0.25):              # track lengths must roughly add up to the video
        return None
    fit, rel, tracks = best
    if dur and fit < 0.1:                                 # same running order: assign start times
        at = 0
        for t in tracks:
            t.start, at = at, at + (t.length or 0)
    return Tracklist(info.get("title") or album, f"MusicBrainz: {rel.get('title')} ({artist})", tracks, dur)


def lookup(info: dict, log_fn=print) -> Optional[Tracklist]:
    """Best online tracklist for a YouTube video's info dict, or None."""
    found = []
    for name, fn in (("MixesDB", mixesdb), ("MusicBrainz", musicbrainz)):
        log_fn(f"Searching {name}...")
        try:
            tl = fn(info)
        except Exception as e:
            log.debug("%s lookup failed: %s", name, e)
            tl = None
        if tl:
            found.append(tl)
            if not tl.looks_incomplete():
                break
    if not found:
        return None
    return max(found, key=lambda t: (len(t.tracks), -t.unknown))
