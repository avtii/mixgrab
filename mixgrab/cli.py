"""mixgrab: YouTube mix URL -> tracklist -> Soulseek downloads."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import shutil
import sys
import tomllib
from pathlib import Path

from .tracklist import Tracklist, fetch_tracklist, load_tracklist_file

CONFIG_DIR = Path(os.environ.get("MIXGRAB_HOME", Path.home() / ".mixgrab"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

DEFAULT_CONFIG = """\
# mixgrab configuration
download_dir = "{download_dir}"

[soulseek]
# A dedicated account, separate from your SoulseekQt login, so both can be online at once.
# Soulseek creates the account on first login. Change these if you want your own name.
username = "{username}"
password = "{password}"
listen_port = 61234
share_downloads = true      # share the download folder back (many peers refuse non-sharers)

[match]
prefer_lossless = true
min_bitrate = 192           # reject mp3s below this
max_lossless_mb = 250
search_seconds = 12         # how long to collect results per query
parallel_searches = 2
parallel_downloads = 4
max_attempts = 6            # different peers to try per track
queue_timeout = 120         # give up on a peer whose queue doesn't move (s)
stall_timeout = 60          # give up on a transfer with no progress (s)

[identify]
auto = true                 # identify from audio when the tracklist is missing or incomplete
step = 45                   # seconds between sampled clips
min_hits = 2                # a song must match in this many clips to count
max_avg_track_len = 600     # tracklist with fewer tracks than duration/this = incomplete
"""

log = logging.getLogger("mixgrab")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG.format(
            download_dir=str(Path.home() / "Music" / "mixgrab").replace("\\", "/"),
            username=f"mixgrab_{secrets.token_hex(3)}",
            password=secrets.token_urlsafe(12),
        ), encoding="utf-8")
        print(f"Created config at {CONFIG_PATH}")
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def mix_folder(cfg: dict, title: str) -> str:
    from .soulseek import safe_name
    d = os.path.join(os.path.expandvars(os.path.expanduser(cfg["download_dir"])), safe_name(title))
    os.makedirs(d, exist_ok=True)
    return d


async def identify_into(tl: Tracklist, url: str, cfg: dict, out_dir: str) -> None:
    from .identify import download_audio, hits_to_tracks, merge, scan
    from .ui import IdentifyProgress, console, fmt_ts
    ic = cfg["identify"]
    work = os.path.join(out_dir, ".identify")
    with IdentifyProgress() as ip:
        audio = await asyncio.to_thread(download_audio, url, work, ip.on_audio)
        duration = tl.duration
        if not duration:
            import yt_dlp
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as y:
                duration = int(y.extract_info(url, download=False).get("duration") or 0)
        hits = await scan(audio, duration, ic["step"], progress=ip.on_clips)
    with open(os.path.join(work, "hits.json"), "w", encoding="utf-8") as f:
        json.dump([h.__dict__ for h in hits], f, indent=1, ensure_ascii=False)
    found, maybe = hits_to_tracks(hits, ic["min_hits"], duration)
    before = len(tl.tracks)
    tl.tracks = merge(tl.tracks, found)
    tl.found_in += f" + audio ID ({len(tl.tracks) - before} added)"
    if maybe:
        console.print("[dim]Low-confidence (heard once, skipped):[/]")
        for h in maybe:
            console.print(f"[dim]  ~{fmt_ts(h.at)}  {h.artist} – {h.title}[/]")
    console.print()


async def run(args) -> int:
    from .ui import DownloadProgress, console, header, summary, tracklist_table
    cfg = load_config()
    if args.out:
        cfg["download_dir"] = args.out

    with console.status("Reading the video and looking for a tracklist…"):
        if args.tracklist:
            tl = load_tracklist_file(args.tracklist, args.title or "mix")
        else:
            tl = await asyncio.to_thread(fetch_tracklist, args.url, lambda *a: None)
    header(args.title or tl.source_title, args.url)

    out_dir = mix_folder(cfg, args.title or tl.source_title)
    ic = cfg["identify"]
    want_id = args.identify or (ic["auto"] and not args.no_identify
                                and tl.looks_incomplete(ic["max_avg_track_len"]))
    if want_id and not args.tracklist:
        why = "requested" if args.identify else (
            "no tracklist found" if not tl.tracks else "tracklist looks incomplete")
        console.print(f"[yellow]Identifying songs from the audio[/] [dim]({why})[/]")
        await identify_into(tl, args.url, cfg, out_dir)

    tracklist_table(tl)
    with open(os.path.join(out_dir, "tracklist.txt"), "w", encoding="utf-8") as f:
        f.write(f"{tl.source_title}\n{args.url}\nsource: {tl.found_in}\n\n")
        f.writelines(f"{t.index:02d}. {t.label}\n" for t in tl.tracks)

    if not tl.tracks:
        console.print("[red]No tracks found.[/] Pass --tracklist FILE with 'Artist - Title' lines, or try --identify.")
        return 1
    if args.dry_run:
        return 0
    tracks = tl.tracks
    if args.only:
        keep = {int(x) for x in args.only.split(",")}
        tracks = [t for t in tl.tracks if t.index in keep]

    from .soulseek import Downloader
    results = {}
    dl = Downloader(cfg, os.path.expandvars(os.path.expanduser(cfg["download_dir"])))
    with console.status(f"Connecting to Soulseek as {cfg['soulseek']['username']}…"):
        await dl.__aenter__()
    try:
        with DownloadProgress(tracks) as ui:
            dl.on_status = ui.status

            async def one(t):
                try:
                    results[t.index] = await dl.fetch(t, out_dir)
                except Exception as e:
                    log.debug("[%02d] error", t.index, exc_info=True)
                    results[t.index] = (None, f"error: {e}")
                path, msg = results[t.index]
                if path:
                    ui.status(t.index, "skip" if msg == "already downloaded" else "done",
                              note=os.path.splitext(path)[1].lstrip("."))
                else:
                    ui.status(t.index, "missing", note=msg)
            await asyncio.gather(*(one(t) for t in tracks))
    finally:
        with console.status("Disconnecting…"):
            await dl.__aexit__(None, None, None)

    if not args.keep_audio:
        shutil.rmtree(os.path.join(out_dir, ".identify"), ignore_errors=True)
    missing = [t for t in tracks if not results[t.index][0]]
    with open(os.path.join(out_dir, "playlist.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tl.tracks:
            r = results.get(t.index)
            if r and r[0]:
                f.write(f"#EXTINF:-1,{t.label}\n{os.path.basename(r[0])}\n")
    with open(os.path.join(out_dir, "missing.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{t.index:02d}. {t.label}  ({results[t.index][1]})\n" for t in missing)
    summary(tracks, results, out_dir)
    return 0 if not missing else 2


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="mixgrab", description=__doc__)
    p.add_argument("url", help="YouTube video or playlist URL")
    p.add_argument("--out", help="download root (overrides config)")
    p.add_argument("--title", help="folder name for this mix")
    p.add_argument("--tracklist", help="text file with 'Artist - Title' lines (skip auto-detection)")
    p.add_argument("--identify", action="store_true", help="always identify songs from the audio")
    p.add_argument("--no-identify", action="store_true", help="never identify from audio")
    p.add_argument("--only", help="comma-separated track numbers to download, e.g. 1,4,7")
    p.add_argument("--dry-run", action="store_true", help="show the tracklist, don't download")
    p.add_argument("--keep-audio", action="store_true", help="keep the downloaded mix audio")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aioslsk", "asyncio", "aiohttp", "shazamio"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.CRITICAL)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
