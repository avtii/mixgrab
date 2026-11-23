"""mixgrab: YouTube / SoundCloud / Mixcloud mix URL -> tracklist -> Soulseek downloads."""
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

from . import __version__
from .tracklist import Tracklist, fetch_tracklist, is_bot_check, load_tracklist_file, use_cookies
from .youtube import SITES

CONFIG_DIR = Path(os.environ.get("MIXGRAB_HOME", Path.home() / ".mixgrab"))
CONFIG_PATH = CONFIG_DIR / "config.toml"
COOKIES_PATH = CONFIG_DIR / "youtube-cookies.txt"

BOT_HELP = f"""[red]YouTube asked mixgrab to sign in ("confirm you're not a bot").[/]
Give it your YouTube cookies, once:
  [bold]mixgrab --export-cookies[/]   finds them in your browsers (Firefox works best) and saves them
  [bold]mixgrab --cookies cookies.txt URL[/]   cookies.txt exported from a browser signed in to youtube.com
                                       (saved to {COOKIES_PATH}, used automatically from then on)
  [bold]mixgrab --cookies-from-browser firefox URL[/]   read them live from Firefox/Safari/Edge/Chrome"""

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
step = 90                   # seconds between sampled clips (then in-between ones where songs change)
min_hits = 2                # a song must match in this many clips to count
max_avg_track_len = 600     # tracklist with fewer tracks than duration/this = incomplete
# [convert]
# mp3 = true                # MP3 copy next to every FLAC/WAV/AIFF download (original is kept)
# bitrate = 320
# [youtube]
# fallback = true           # tracks not on Soulseek are taken from YouTube, saved as '... [YT].mp3'
# quality = "0"             # LAME VBR 0 (best) .. 9, or a bitrate such as "320"
# cookies = "~/cookies.txt"         # only if YouTube asks to sign in; or run once with --cookies FILE
# cookies_from_browser = "firefox"  # alternative: read cookies live from a browser
# [soundcloud]
# fallback = true           # then SoundCloud (full tracks only, no 30 s previews), saved as '... [SC].mp3'
# Optional fallback recognizers for clips Shazam misses (SoundHound has no public API):
# audd_token = ""           # https://audd.io (free quota)
# [identify.acrcloud]       # https://www.acrcloud.com (best for DJ sets; free trial)
# host = "identify-eu-west-1.acrcloud.com"
# access_key = ""
# access_secret = ""
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


def setup_cookies(cfg: dict, args) -> None:
    """--cookies FILE (remembered), --cookies-from-browser, [youtube] config, or a saved cookies file."""
    yt = cfg.get("youtube", {})
    if args.cookies:
        src = Path(args.cookies).expanduser()
        if not src.is_file():
            raise SystemExit(f"cookies file not found: {src}")
        if src.resolve() != COOKIES_PATH.resolve():
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, COOKIES_PATH)
            print(f"Saved YouTube cookies to {COOKIES_PATH}; later runs use them automatically.")
    browser = args.cookies_from_browser or yt.get("cookies_from_browser")
    file = yt.get("cookies") or (str(COOKIES_PATH) if COOKIES_PATH.is_file() else None)
    if browser and not args.cookies:
        use_cookies(browser=browser)
    elif file:
        use_cookies(file=os.path.expanduser(file))


def export_cookies(browser: str | None) -> int:
    """--export-cookies: find signed-in YouTube cookies in a browser and save them as cookies.txt."""
    from .tracklist import export_youtube_cookies
    print(f"Looking for YouTube cookies in {browser or 'your browsers'}…")
    try:
        r = export_youtube_cookies(COOKIES_PATH, browser)
    except RuntimeError as e:
        print("No usable YouTube cookies found:")
        print("\n".join("  " + line for line in str(e).splitlines()))
        print("\nSign in to youtube.com in Firefox (its cookies can always be read), close it, then run this again.\n"
              "Chrome and Edge lock their cookies while open, and newer Chrome encrypts them for itself only.")
        return 1
    print(f"Found {r['cookies']} YouTube cookies in {r['browser']}"
          + ("" if r["signed_in"] else " (not signed in to YouTube, so they may not help)"))
    print(f"Saved to: {COOKIES_PATH}")
    print("mixgrab uses this file automatically. For the web app, upload this file with "
          "'Add YouTube cookies' in the sidebar.")
    return 0


def check() -> int:
    """Report what mixgrab needs and where it looks."""
    import platform
    from .identify import FFMPEG_HINT, find_ffmpeg
    ok = True
    print(f"mixgrab {__version__} on {platform.system()} {platform.machine()}")
    print(f"config:   {CONFIG_PATH} ({'exists' if CONFIG_PATH.exists() else 'created on first run'})")
    ff = find_ffmpeg(required=False)
    print(f"ffmpeg:   {ff or 'MISSING - install: ' + FFMPEG_HINT.get(sys.platform, 'apt install ffmpeg')}")
    ok &= bool(ff)
    js = shutil.which("deno") or shutil.which("node")
    print(f"JS:       {js or 'MISSING - YouTube needs deno or node: ' + ('winget install DenoLand.Deno' if sys.platform == 'win32' else 'brew install deno')}")
    ok &= bool(js)
    print(f"cookies:  {COOKIES_PATH if COOKIES_PATH.is_file() else 'none (only needed if YouTube asks you to sign in)'}")
    try:
        cfg = load_config() if CONFIG_PATH.exists() else None
        if cfg:
            print(f"download: {os.path.expanduser(cfg['download_dir'])}")
            print(f"soulseek: {cfg['soulseek']['username']}")
    except Exception as e:
        print(f"config error: {e}")
        ok = False
    print("ready" if ok else "fix the MISSING items above")
    return 0 if ok else 1


def mix_folder(cfg: dict, title: str) -> str:
    from .soulseek import safe_name
    d = os.path.join(os.path.expandvars(os.path.expanduser(cfg["download_dir"])), safe_name(title))
    os.makedirs(d, exist_ok=True)
    return d


async def run(args) -> int:
    from .engine import Engine, identify_into, identify_reason, with_defaults
    from .ui import IdentifyProgress, DownloadProgress, console, esc, fmt_ts, header, summary, tracklist_table
    cfg = with_defaults(load_config())
    if args.out:
        cfg["download_dir"] = args.out
    if args.no_youtube:
        cfg["youtube"]["fallback"] = False
    if args.no_soundcloud:
        cfg["soundcloud"]["fallback"] = False
    if args.no_mp3:
        cfg["convert"]["mp3"] = False
    setup_cookies(cfg, args)

    try:
        with console.status("Reading the mix and looking for a tracklist (page, comments, MixesDB, MusicBrainz)…"):
            if args.tracklist:
                tl = load_tracklist_file(args.tracklist, args.title or "mix")
            else:
                tl = await asyncio.to_thread(fetch_tracklist, args.url, lambda *a: None)
    except Exception as e:
        if not is_bot_check(e):
            raise
        console.print(BOT_HELP)
        return 3
    header(args.title or tl.source_title, args.url)

    out_dir = mix_folder(cfg, args.title or tl.source_title)
    ic = cfg["identify"]
    mode = "always" if args.identify else "never" if (args.no_identify or not ic.get("auto", True)) else "auto"
    why = None if args.tracklist else identify_reason(tl, mode, ic)
    id_complete = True
    if why:
        console.print(f"[yellow]Identifying songs from the audio[/] [dim]({esc(why)})[/]")
        with IdentifyProgress() as ip:
            maybe, id_complete = await identify_into(tl, args.url, os.path.join(out_dir, ".identify"), ic,
                                                     ip.on_audio, ip.on_clips)
        if not id_complete:
            console.print("[yellow]Shazam kept rate-limiting, so identification stopped early.[/] "
                          "Run the same command again later: it carries on from where it stopped.")
        if maybe:
            console.print("[dim]Low-confidence (heard once, skipped):[/]")
            for h in maybe:
                console.print(f"[dim]  ~{fmt_ts(h.at)}  {esc(h.artist)} – {esc(h.title)}[/]")
        console.print()

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
    dl = Downloader(cfg, os.path.expandvars(os.path.expanduser(cfg["download_dir"])))
    with console.status(f"Connecting to Soulseek as {cfg['soulseek']['username']}…"):
        try:
            await dl.__aenter__()
        except Exception as e:
            if not (cfg["youtube"]["fallback"] or cfg["soundcloud"]["fallback"]):
                console.print(f"[red]Soulseek login failed:[/] {esc(str(e))}")
                return 1
            console.print(f"[yellow]Soulseek login failed ({esc(str(e))}); using YouTube/SoundCloud only.[/]")
            dl = None
    eng = Engine(cfg, dl)
    results = {}
    try:
        with DownloadProgress(tracks) as ui:
            eng.on_status = ui.status
            if dl:
                dl.on_status = ui.status

            async def one(t):
                try:
                    r = await eng.fetch(t, out_dir)
                except Exception as e:
                    log.debug("[%02d] error", t.index, exc_info=True)
                    from .engine import Fetched
                    r = Fetched(None, None, "", f"error: {e}")
                results[t.index] = r
                if r.path:
                    note = os.path.splitext(r.path)[1].lstrip(".") + (" +mp3" if r.mp3 else "") + (SITES[r.source].tag if r.source in SITES else "")
                    ui.status(t.index, "skip" if r.source == "cache" else "done", note=note)
                else:
                    ui.status(t.index, "missing", note=r.msg)
            await asyncio.gather(*(one(t) for t in tracks))
    finally:
        if dl:
            with console.status("Disconnecting…"):
                await dl.__aexit__(None, None, None)

    if not args.keep_audio and id_complete:
        shutil.rmtree(os.path.join(out_dir, ".identify"), ignore_errors=True)
    missing = [t for t in tracks if not results[t.index].path]
    with open(os.path.join(out_dir, "playlist.m3u"), "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for t in tl.tracks:
            r = results.get(t.index)
            if r and r.path:
                f.write(f"#EXTINF:-1,{t.label}\n{os.path.basename(r.path)}\n")
    with open(os.path.join(out_dir, "missing.txt"), "w", encoding="utf-8") as f:
        f.writelines(f"{t.index:02d}. {t.label}  ({results[t.index].msg})\n" for t in missing)
    summary(tracks, results, out_dir)
    return 0 if not missing else 2


def sync(args, cfg: dict) -> int:
    """mixgrab --sync D: copy the library to a USB drive, only what is new or changed."""
    from rich.progress import BarColumn, DownloadColumn, Progress, TextColumn, TransferSpeedColumn
    from . import sync as sy
    from .ui import console, esc
    library = os.path.expandvars(os.path.expanduser(args.out or cfg["download_dir"]))
    if not os.path.isdir(library):
        console.print(f"[red]Library folder not found:[/] {esc(library)}")
        return 1
    dest = sy.resolve_dest(args.sync, library)
    drive = os.path.splitdrive(dest)[0]
    if drive and not os.path.exists(drive + os.sep):
        console.print(f"[red]Drive {esc(drive)} isn't there.[/] Plug in the USB stick, or check the letter.")
        return 1
    lib, dst = os.path.normcase(os.path.abspath(library)), os.path.normcase(os.path.abspath(dest))
    if os.path.splitdrive(lib)[0] == os.path.splitdrive(dst)[0] and os.path.commonpath([lib, dst]) in (lib, dst):
        console.print("[red]The destination and the library overlap.[/] Pick another folder or drive.")
        return 1
    with console.status("Comparing the library with the drive…"):
        p = sy.plan(library, dest, mp3_only=args.mp3_only, mirror=args.mirror)
    console.print(f"[bold]{esc(library)}[/] → [bold]{esc(dest)}[/]")
    console.print(f"{len(p.copy)} to copy ({p.bytes / 1e6:,.0f} MB), {p.same} already there"
                  + (f", {len(p.remove)} on the drive but not in the library" if args.mirror else "")
                  + (", MP3 copies instead of lossless files" if args.mp3_only else ""))
    probe = dest
    while not os.path.exists(probe) and os.path.dirname(probe) != probe:
        probe = os.path.dirname(probe)
    free = shutil.disk_usage(probe).free
    if p.bytes > free:
        console.print(f"[red]Not enough space:[/] needs {p.bytes / 1e6:,.0f} MB, the drive has {free / 1e6:,.0f} MB free."
                      + ("" if args.mp3_only else " Try --mp3-only."))
        return 1
    if args.dry_run:
        for src, _, size in p.copy[:40]:
            console.print(f"  [green]+[/] {esc(os.path.relpath(src, library))} [dim]{size / 1e6:.1f} MB[/]")
        for f in p.remove[:40]:
            console.print(f"  [red]-[/] {esc(os.path.relpath(f, dest))}")
        if len(p.copy) > 40 or len(p.remove) > 40:
            console.print("  [dim]…[/]")
        return 0
    if p.remove:
        if not sys.stdin.isatty():
            console.print("[red]--mirror would delete files; run it in a terminal to confirm.[/]")
            return 1
        try:
            ans = console.input(f"[yellow]Delete {len(p.remove)} files from {esc(dest)} that aren't in the library?[/] [y/N] ")
        except EOFError:
            ans = ""
        if ans.strip().lower() not in ("y", "yes"):
            console.print("[dim]Nothing deleted.[/]")
            p.remove = []
    if not (p.copy or p.remove or p.playlists):
        console.print("[green]Already in sync.[/]")
        return 0
    with Progress(TextColumn("{task.description}"), BarColumn(bar_width=30), DownloadColumn(),
                  TransferSpeedColumn(), TextColumn("[dim]{task.fields[name]}"), console=console) as bar:
        task = bar.add_task("Copying", total=p.bytes or 1, name="")
        errors = sy.run(p, dest, lambda done, name: bar.update(task, completed=done, name=name[:50]))
    for f, e in errors[:20]:
        console.print(f"  [red]✗[/] {esc(os.path.relpath(f, library) if f.startswith(library) else f)} [dim]— {esc(e)}[/]")
    ok = len(p.copy) + len(p.remove) - len(errors)
    console.print(f"[bold {'yellow' if errors else 'green'}]Synced[/] {ok} files"
                  + (f", {len(errors)} failed" if errors else "")
                  + ". [dim]Eject the drive before unplugging it.[/]")
    return 2 if errors else 0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(prog="mixgrab", description=__doc__)
    p.add_argument("url", nargs="?", help="YouTube video/playlist, SoundCloud or Mixcloud URL")
    p.add_argument("--version", action="version", version=f"mixgrab {__version__}")
    p.add_argument("--check", action="store_true", help="check ffmpeg, JS runtime, cookies and config, then exit")
    p.add_argument("--cookies", metavar="FILE", help="YouTube cookies.txt (saved and reused) if YouTube asks to sign in")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="read YouTube cookies from firefox, safari, edge, chrome, brave…")
    p.add_argument("--out", help="download root (overrides config)")
    p.add_argument("--title", help="folder name for this mix")
    p.add_argument("--tracklist", help="text file with 'Artist - Title' lines (skip auto-detection)")
    p.add_argument("--identify", action="store_true", help="always identify songs from the audio")
    p.add_argument("--no-identify", action="store_true", help="never identify from audio")
    p.add_argument("--only", help="comma-separated track numbers to download, e.g. 1,4,7")
    p.add_argument("--dry-run", action="store_true", help="show the tracklist, don't download")
    p.add_argument("--keep-audio", action="store_true", help="keep the downloaded mix audio")
    p.add_argument("--no-youtube", action="store_true", help="don't fall back to YouTube for tracks Soulseek lacks")
    p.add_argument("--no-soundcloud", action="store_true", help="don't fall back to SoundCloud for tracks Soulseek lacks")
    p.add_argument("--no-mp3", action="store_true", help="don't make MP3 copies of lossless downloads")
    p.add_argument("--sync", metavar="DEST",
                   help="copy the library to a USB drive or folder, only new or changed files, then exit. "
                        "A drive letter (D or D:) means D:/<library folder name>")
    p.add_argument("--mp3-only", action="store_true", help="with --sync: send the MP3 copy instead of FLAC/WAV when there is one")
    p.add_argument("--mirror", action="store_true", help="with --sync: also delete files on the drive that the library no longer has (asks first)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--export-cookies", nargs="?", const="auto", metavar="BROWSER",
                   help="find your YouTube cookies in a browser (all of them, or the one named), save them to "
                        f"{COOKIES_PATH} for mixgrab and for uploading to the web app, then exit")
    args = p.parse_args()
    if args.check:
        sys.exit(check())
    if args.export_cookies:
        sys.exit(export_cookies(None if args.export_cookies == "auto" else args.export_cookies))
    if args.sync:
        sys.exit(sync(args, load_config()))
    if not args.url:
        p.print_help()
        sys.exit(1)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("aioslsk", "asyncio", "aiohttp", "aiohttp_retry", "shazamio"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if args.verbose else logging.CRITICAL)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
