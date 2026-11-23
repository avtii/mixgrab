FROM python:3.13-slim

# ffmpeg: clips for audio ID, FLAC->MP3, YouTube audio extraction. deno: YouTube's JS challenges (yt-dlp).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY pyproject.toml README.md ./
COPY mixgrab ./mixgrab
RUN pip install --no-cache-dir ".[web]" "yt-dlp[default]" bgutil-ytdlp-pot-provider \
 && useradd --create-home --uid 1000 mixgrab \
 && mkdir -p /data && chown mixgrab /data

USER mixgrab
ENV MIXGRAB_DATA=/data PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080 61234 61235
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["mixgrab-web"]
