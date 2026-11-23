# mixgrab

YouTube DJ mix / set / playlist URL → tracklist → downloads from Soulseek.

```
mixgrab "https://www.youtube.com/watch?v=-AuOAMND-qY"            # find tracklist + download
mixgrab URL --dry-run                                            # just show the tracklist
mixgrab URL --identify                                           # force audio identification
mixgrab URL --tracklist my_list.txt                              # use your own "Artist - Title" lines
mixgrab URL --only 3,7                                           # download only some tracks
```

## How the tracklist is found
1. Playlist URL → each video title is a track.
2. Video chapters → description → comments (uploader or pinned comment first, then the one with the most tracks).
3. **Audio identification**: runs automatically when nothing is found, when the list has `ID - ID` lines,
   or when there are too few tracks for the mix length (avg > 10 min/track). The program downloads the mix audio,
   cuts a 12 s clip every 45 s and runs each through Shazam (`shazamio`). A song must match in ≥ 2 clips to count.
   One-off matches are printed as low-confidence and not downloaded. The results are merged with whatever partial list existed.

## Downloading
Uses its own Soulseek login through `aioslsk`, separate from SoulseekQt, so both can run at once.
For each track: search (with fallback queries), score files by artist/title match, unwanted version words (remix/live/edit…),
format (FLAC > MP3 320 > …, MP3 < 192 kbps rejected), duration vs. the track's length in the mix, and peer free slot/queue/speed.
Then try up to 6 peers, dropping one whose queue doesn't move in 120 s or whose transfer stalls for 60 s.

Output: `<download_dir>/<mix title>/NN - Artist - Title.ext`, plus `tracklist.txt`, `playlist.m3u` and `missing.txt`.
Re-running skips tracks already downloaded.

## Config
`~/.mixgrab/config.toml` is created on the first run with a random dedicated Soulseek account,
`download_dir`, matching and identification settings. Requires ffmpeg for `--identify` (`winget install Gyan.FFmpeg`).
