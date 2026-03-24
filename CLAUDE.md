# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A FastAPI web app that downloads Spotify playlists via Soulseek (using slskd as the Soulseek client). The user pastes a Spotify playlist URL, and the app searches Soulseek for each track, downloads the best quality file (FLAC or 320kbps MP3), converts FLAC to MP3 via ffmpeg, and tags the output with metadata fetched from Spotify.

## Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run the dev server (hot reload)
python app.py
# or
uvicorn app:app --host 0.0.0.0 --port 1337 --reload
```

App runs at http://localhost:1337. Requires slskd running (default: http://localhost:5030) and ffmpeg installed.

## Running with Docker

```bash
# Requires SOULSEEK_USERNAME, SOULSEEK_PASSWORD, SLSKD_API_KEY in environment
docker compose up -d
```

## Configuration

Config is stored in `/config/settings.json` (in Docker) or a path from `CONFIG_FILE` env var. Settings can also come from env vars or a `.env` file. Environment variables take priority over saved config. The UI at `/` includes an onboarding wizard if not yet configured.

Required settings: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SLSKD_API_KEY`.

Key optional settings:
- `SLSKD_HOST` — default `http://localhost:5030`
- `SLSKD_DOWNLOAD_DIR` — path where slskd writes downloaded files (must be accessible to this app)
- `DOWNLOAD_DIR` — where finished MP3s are written (default `./downloads`)
- `SEARCH_TIMEOUT_MS` — default 30000
- `MIN_BITRATE` — default 192 (currently unused in scoring; scoring logic is in `downloader.py`)

## Architecture

```
app.py              FastAPI app — API routes, WebSocket, lifespan init
config.py           Settings (pydantic-settings), load/save to JSON file
core/
  models.py         Pydantic models: TrackInfo, TrackJob, PlaylistJob, TrackStatus
  spotify.py        SpotifyClient — fetches playlist tracks + audio features (BPM, key)
  slskd.py          SlskdClient — async HTTP wrapper around the slskd REST API
  downloader.py     DownloadOrchestrator — coordinates the full search→download→tag pipeline
  tagger.py         Tagger — writes ID3/FLAC tags and embeds 600×600 cover art
static/
  index.html        Single-page UI (no framework)
  app.js            Frontend JS — calls REST API, opens WebSocket for live progress
  style.css
```

### Download Pipeline (per track)

1. `SpotifyClient.get_playlist_tracks()` — fetch all tracks + audio features (BPM, key, Camelot)
2. `DownloadOrchestrator.create_job()` — build a `PlaylistJob` with `TrackJob` per track
3. `process_job()` runs in the background (FastAPI `BackgroundTasks`), sequentially per track:
   - Search slskd with up to 2 query variants
   - Score results via `score_file()` — prefers FLAC > 320kbps MP3; rejects low bitrate, duration mismatch >15s, filename not matching artist+title
   - Enqueue best file for download via slskd API
   - Poll slskd download state every 5s (up to 10 min)
   - Find file on disk by walking `SLSKD_DOWNLOAD_DIR`
   - Convert FLAC→MP3 (320kbps) with ffmpeg subprocess if needed
   - Tag output file with Spotify metadata via `Tagger`
4. Job status is live-streamed to the browser via WebSocket at `/ws/jobs/{job_id}`

### Job State

Jobs are in-memory only (lost on restart). Job/track statuses: `pending → searching → found → downloading → tagging → complete` (or `failed`/`not_found`). Jobs can be stopped and resumed; resumption skips already-completed tracks.

### Key Design Details

- All output is MP3 regardless of source format (FLAC is converted, MP3 is copied as-is)
- Tags written: title, artist, album, track number, year, BPM, musical key (TKEY), Camelot key (TXXX `INITIAL_KEY`), cover art (600×600 JPEG)
- File scoring rejects anything that isn't FLAC or 320kbps MP3; duration must be within 15 seconds of Spotify's value; filename must fuzzy-match artist + title
- `SLSKD_DOWNLOAD_DIR` must be the path *inside this container* to the directory where slskd writes files (mapped via Docker volume)
