# mixgrab

Takes a YouTube DJ mix, set, album or playlist URL, finds its tracklist and downloads the tracks from Soulseek. When a track isn't on Soulseek, it falls back to YouTube.

It comes in two forms:
- **CLI**: `mixgrab URL`.
- **Private web app**: `mixgrab-web`, for a single owner. It runs on the same engine as the CLI.

```
mixgrab "https://www.youtube.com/watch?v=-AuOAMND-qY"            # find tracklist + download
mixgrab URL --dry-run                                            # just show the tracklist
mixgrab URL --identify                                           # force audio identification
mixgrab URL --tracklist my_list.txt                              # use your own "Artist - Title" lines
mixgrab URL --only 3,7                                           # download only some tracks
mixgrab URL --no-youtube                                         # Soulseek only, no [YT] fallback
mixgrab URL --no-mp3                                             # keep FLAC/WAV only, no MP3 copy
```

## How the tracklist is found
1. **Playlist URL**: each video title becomes a track.
2. **The video itself**: it looks at the chapters first, then the description, then the comments. In the comments it tries the uploader's or the pinned comment first, then the comment with the most tracks. Chapters that contain only a title (as on album uploads) take the artist from the video.
3. **Online lookup**: this runs when the list is missing or looks incomplete.
   - **MixesDB**: searched by video ID, then by shorter and shorter title queries. A page that links the video counts as verified.
   - **MusicBrainz**: used for albums. It takes the release whose total length is closest to the video's.

   The online list replaces the YouTube one only if one of these holds:
   - it has more tracks;
   - it is verified and has at least 60% as many tracks;
   - it has the same number of tracks but fewer unknown ones.
4. **Audio identification**: this runs in any of these cases:
   - nothing was found;
   - the list has `ID - ID` lines or `?` lines;
   - there are too few tracks for the mix length (more than 10 minutes per track on average).

   How it works:
   - It downloads the mix audio and cuts a 12-second clip every 45 seconds.
   - Each clip goes to Shazam (`shazamio`) first.
   - If Shazam misses, the clip goes to **ACRCloud** and **AudD**, if their keys are set in the config. SoundHound has no public API, so it is not used.
   - A song counts only if it matches in at least 2 clips. Songs heard only once are listed separately and are not downloaded.

## Downloading
Each track is downloaded in this order:
1. **Soulseek**, through mixgrab's own `aioslsk` login. This is separate from SoulseekQt, so both can run at once.
   - Files are scored on these points:
     - how well the artist and title match;
     - unwanted version words (remix, live, edit and similar);
     - format: FLAC is preferred over MP3 320, and MP3 below 192 kbps is rejected;
     - duration;
     - the peer's free slots, queue and speed.
   - It tries up to 6 peers. A peer is dropped if its queue doesn't move for 120 seconds or its transfer stalls for 60 seconds.
2. **YouTube fallback**, if Soulseek finds nothing.
   - It searches for `Artist - Title` and prefers "Artist - Topic" channels and "official audio" uploads.
   - It rejects mixes, live versions, covers and reactions, and checks the duration.
   - The audio is saved as MP3 with **`[YT]`** in the file name.
3. **MP3 copy**: every lossless download (FLAC, WAV, AIFF and similar) also gets a 320 kbps MP3 copy next to it, with the tags kept.

Output goes to `<download_dir>/<mix title>/`. It contains:
- `NN - Artist - Title.flac` and `NN - Artist - Title.mp3`;
- `NN - Artist - Title [YT].mp3` for tracks that came from YouTube;
- `tracklist.txt`, `playlist.m3u` and `missing.txt`.

Re-running the command skips tracks that are already downloaded. If the Soulseek login fails, the run continues using YouTube only.

## Config
The file `~/.mixgrab/config.toml` is created on the first run. It contains:
- a random, dedicated Soulseek account;
- `download_dir`;
- matching and identification settings.

ffmpeg is required (`winget install Gyan.FFmpeg`).

Optional sections:
```toml
[convert]
mp3 = true          # MP3 copy of lossless files
bitrate = 320

[youtube]
fallback = true
quality = "0"       # yt-dlp/LAME VBR quality, 0 = best

[identify]
audd_token = ""     # https://audd.io
[identify.acrcloud]
host = "identify-eu-west-1.acrcloud.com"
access_key = ""
access_secret = ""
```

## Web app (private, single owner)
`pip install ".[web]"`, then run `MIXGRAB_PASSWORD=... mixgrab-web`. The app listens on port 8080.

There is no registration: only one login exists, and it is set from the environment.

The web app and the CLI share `mixgrab/engine.py`, which does the tracklist, identification, Soulseek, YouTube fallback and MP3 steps. Soulseek allows one session per account, so the web app keeps a single Soulseek connection open and runs every job through it. It does not start a separate CLI process for each job.

Features:
- job history;
- live progress for the identification and download phases;
- per-track play, download, edit and retry;
- a download link for the MP3 copy of each track;
- a zip of the whole job;
- download links that expire;
- a cache that is cleaned automatically.

| Variable | Default | Meaning |
|---|---|---|
| `MIXGRAB_PASSWORD` | (required, 12+ characters) | owner password |
| `MIXGRAB_USER` | `owner` | owner login |
| `MIXGRAB_DATA` | `./data` (`/data` in Docker) | jobs, files, cache |
| `SLSK_USERNAME` / `SLSK_PASSWORD` | generated and saved | Soulseek account |
| `SLSK_PORT` | 61234 | Soulseek listen port (publish it, plus port +1) |
| `CACHE_MAX_GB` / `CACHE_HIGH` / `CACHE_FREE` | 15 / 0.92 / 0.12 | when the cache reaches 92% of its budget, the oldest 12% is deleted |
| `LINK_TTL_MIN` | 1440 | how long a signed file link stays valid |
| `CONVERT_MP3` / `MP3_BITRATE` | 1 / 320 | MP3 copies |
| `YOUTUBE_FALLBACK` / `YOUTUBE_QUALITY` | 1 / 0 | YouTube fallback |
| `YTDLP_COOKIES` | (none) | cookies.txt path, for when YouTube blocks the server |
| `AUDD_TOKEN`, `ACR_HOST`, `ACR_ACCESS_KEY`, `ACR_ACCESS_SECRET` | (none) | extra audio recognizers |

### Deploy
`.github/workflows/deploy.yml` runs on every push to `main`. It does three things:
1. Checks the code and builds `ghcr.io/avtii/mixgrab`.
2. Connects to the server over SSH and writes `/opt/mixgrab/compose.yml` and `.env`. If the server has a file `/opt/mixgrab/extra.env`, its lines are appended to `.env`.
3. Pulls the new image and restarts the container. On the first deploy it also adds `mixgrab.roketz.app` to the burrball Caddyfile.

Repository secrets:
- Required: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_KEY` and `MIXGRAB_PASSWORD`.
- Optional: `DEPLOY_PORT`, `DEPLOY_PASSPHRASE`, `AUDD_TOKEN`, `ACR_ACCESS_KEY` and `ACR_ACCESS_SECRET`.

Repository variables (optional): `ACR_HOST` and `MIXGRAB_DOMAIN`.

If the deploy secrets are missing, the image is still published but nothing is deployed.
