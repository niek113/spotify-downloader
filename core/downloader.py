import asyncio
import logging
import os
import re
import shutil
from typing import Optional
from uuid import uuid4

import httpx

from config import Settings
from core.models import PlaylistJob, TrackJob, TrackInfo, TrackStatus
from core.slskd import SlskdClient
from core.spotify import SpotifyClient
from core.tagger import Tagger

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip(". ")
    return name or "unknown"


def score_file(file: dict, response: dict, target_duration_ms: int) -> float:
    score = 0.0
    fname = file.get("filename", "").lower()

    if fname.endswith(".flac"):
        score += 90
    elif fname.endswith(".mp3"):
        bitrate = file.get("bitRate", 0) or 0
        if bitrate >= 320:
            score += 100
        elif bitrate >= 256:
            score += 70
        elif bitrate >= 192:
            score += 50
        elif bitrate > 0:
            return -1
        else:
            # Unknown bitrate — allow but score low
            score += 30
    else:
        return -1

    length_s = file.get("length", 0) or 0
    if length_s and target_duration_ms:
        deviation_ms = abs(length_s * 1000 - target_duration_ms)
        if deviation_ms > 30000:
            return -1
        elif deviation_ms < 5000:
            score += 20
        elif deviation_ms < 15000:
            score += 10

    if response.get("freeUploadSlots", 0) > 0:
        score += 15
    speed = response.get("uploadSpeed", 0) or 0
    if speed > 1_000_000:
        score += 10
    elif speed > 500_000:
        score += 5
    queue_len = response.get("queueLength", 999) or 999
    if queue_len < 5:
        score += 10
    elif queue_len < 20:
        score += 5

    file_size = file.get("size", 0) or 0
    if file_size > 3_000_000:
        score += 5

    return score


class DownloadOrchestrator:
    def __init__(
        self,
        spotify: SpotifyClient,
        slskd: SlskdClient,
        tagger: Tagger,
        settings: Settings,
    ):
        self.spotify = spotify
        self.slskd = slskd
        self.tagger = tagger
        self.settings = settings
        self.jobs: dict[str, PlaylistJob] = {}
        self._stop_flags: dict[str, bool] = {}

    def create_job(self, playlist_url: str) -> PlaylistJob:
        job_id = str(uuid4())
        playlist_name, tracks = self.spotify.get_playlist_tracks(playlist_url)
        job = PlaylistJob(
            job_id=job_id,
            playlist_name=playlist_name,
            playlist_url=playlist_url,
            tracks=[TrackJob(track=t) for t in tracks],
        )
        self.jobs[job_id] = job
        return job

    def stop_job(self, job_id: str) -> bool:
        """Signal a job to stop after the current track."""
        job = self.jobs.get(job_id)
        if not job or job.status not in ("running",):
            return False
        self._stop_flags[job_id] = True
        return True

    def resume_job(self, job: PlaylistJob) -> None:
        """Resume a stopped job from where it left off."""
        self._stop_flags[job.job_id] = False
        job.status = "running"

    async def process_job(self, job: PlaylistJob) -> None:
        """Process all tracks sequentially, starting from current_track_index."""
        self._stop_flags.setdefault(job.job_id, False)
        start = job.current_track_index

        for i in range(start, len(job.tracks)):
            if self._stop_flags.get(job.job_id, False):
                job.status = "stopped"
                job.current_track_index = i
                logger.info(f"Job {job.job_id} stopped at track {i}")
                return

            track_job = job.tracks[i]
            # Skip already completed/failed tracks (from previous run)
            if track_job.status in (TrackStatus.COMPLETE, TrackStatus.FAILED, TrackStatus.NOT_FOUND):
                continue

            try:
                await self._search_and_download(job, track_job)
            except Exception as e:
                logger.exception(f"Track {i} failed: {e}")
                track_job.status = TrackStatus.FAILED
                track_job.error = str(e)

            job.current_track_index = i + 1
            # Small delay between tracks to be gentle on slskd
            await asyncio.sleep(2.0)
        job.status = "complete"

    async def _search_and_download(
        self, job: PlaylistJob, track_job: TrackJob
    ) -> None:
        track = track_job.track
        track_job.status = TrackStatus.SEARCHING
        logger.info(f"Searching for: {track.artist} - {track.title}")

        # Try search queries in order
        queries = [
            f"{track.artist} {track.title}",
            track.title,
        ]

        best = None
        for query in queries:
            try:
                search_id = await self.slskd.search(query, self.settings.search_timeout_ms)
                track_job.search_id = search_id
                responses = await self.slskd.wait_for_search(search_id, max_wait=45.0)
                await self.slskd.delete_search(search_id)
                best = self._select_best_file(responses, track.duration_ms)
                if best is not None:
                    logger.info(f"Found match for '{query}' from user {best[0]}")
                    break
                logger.info(f"No results for query: '{query}'")
            except Exception as e:
                logger.warning(f"Search failed for '{query}': {e}")
                continue

        if best is None:
            track_job.status = TrackStatus.NOT_FOUND
            logger.warning(f"Not found: {track.artist} - {track.title}")
            return

        username, file_info = best
        track_job.status = TrackStatus.FOUND
        track_job.slskd_username = username
        track_job.slskd_filename = file_info["filename"]

        # Enqueue download
        track_job.status = TrackStatus.DOWNLOADING
        try:
            await self.slskd.enqueue_download(username, [file_info])
        except Exception as e:
            track_job.status = TrackStatus.FAILED
            track_job.error = f"Failed to enqueue download: {e}"
            return

        # Wait for download to complete
        download_ok = await self._wait_for_download(track_job, username, file_info, job_id=job.job_id)
        if not download_ok:
            return  # status already set in _wait_for_download

        # Wait for file to be flushed to disk
        await asyncio.sleep(5.0)

        # Tag and move file
        track_job.status = TrackStatus.TAGGING
        ext = self._get_extension(file_info["filename"])
        output_path = self._build_output_path(job.playlist_name, track, ext)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        source_path = self._find_downloaded_file(username, file_info["filename"])
        if source_path and os.path.exists(source_path):
            logger.info(f"Found file at: {source_path}")
            shutil.copy2(source_path, output_path)
        else:
            track_job.status = TrackStatus.FAILED
            dir_info = self._debug_list_dir(self.settings.slskd_download_dir)
            track_job.error = (
                f"File not found on disk. "
                f"SLSKD_DOWNLOAD_DIR={self.settings.slskd_download_dir}, "
                f"looking for: {file_info['filename']}, "
                f"dir: {dir_info}"
            )
            logger.error(track_job.error)
            return

        try:
            await self.tagger.tag_file(output_path, track)
        except Exception as e:
            logger.warning(f"Tagging failed: {e}")
            track_job.error = f"Tagging failed: {e}"

        track_job.output_path = output_path
        track_job.status = TrackStatus.COMPLETE
        logger.info(f"Complete: {track.artist} - {track.title} -> {output_path}")

    def _select_best_file(
        self, responses: list[dict], duration_ms: int
    ) -> Optional[tuple[str, dict]]:
        candidates: list[tuple[float, str, dict]] = []
        for resp in responses:
            username = resp.get("username", "")
            for f in resp.get("files", []):
                s = score_file(f, resp, duration_ms)
                if s > 0:
                    candidates.append((s, username, f))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, username, file_info = candidates[0]
        logger.info(
            f"Selected: {file_info.get('filename', '?')} "
            f"(score={candidates[0][0]}, bitrate={file_info.get('bitRate', '?')}, "
            f"user={username})"
        )
        return username, file_info

    async def _wait_for_download(
        self,
        track_job: TrackJob,
        username: str,
        file_info: dict,
        timeout: float = 600,
        job_id: str = "",
    ) -> bool:
        """Wait for download to complete. Returns True on success."""
        target_filename = file_info["filename"]
        elapsed = 0.0

        while elapsed < timeout:
            await asyncio.sleep(5.0)
            elapsed += 5.0

            if job_id and self._stop_flags.get(job_id, False):
                track_job.status = TrackStatus.PENDING
                track_job.progress_pct = 0.0
                return False

            try:
                directories = await self.slskd.get_user_downloads(username)
            except Exception as e:
                logger.warning(f"Error fetching downloads for {username}: {e}")
                continue

            # Search through all directories and files for our target
            found = False
            for directory in directories:
                if not isinstance(directory, dict):
                    continue
                files = directory.get("files", [])
                if not isinstance(files, list):
                    continue
                for f in files:
                    if not isinstance(f, dict):
                        continue
                    if f.get("filename") != target_filename:
                        continue

                    found = True
                    state = str(f.get("state", ""))
                    logger.info(f"Download state for {target_filename}: {state}")

                    # Check for completed states
                    # Exact state strings from slskd API:
                    # "Completed, Succeeded"
                    # "Completed, Cancelled"
                    # "Completed, TimedOut"
                    # "Completed, Errored"
                    # "Completed, Rejected"
                    if "Completed" in state:
                        if "Succeeded" in state:
                            track_job.progress_pct = 100.0
                            return True
                        # Any other completed state is a failure
                        track_job.status = TrackStatus.FAILED
                        track_job.error = f"Download failed with state: {state}"
                        logger.error(f"Download failed: {state}")
                        return False

                    # Still in progress — update progress
                    size = f.get("size", 0) or 1
                    transferred = f.get("bytesTransferred", 0) or 0
                    if size > 0:
                        track_job.progress_pct = (transferred / size) * 100

            if not found:
                logger.debug(
                    f"File not yet in downloads list ({elapsed:.0f}s elapsed)"
                )

        track_job.status = TrackStatus.FAILED
        track_job.error = "Download timed out after 10 minutes"
        return False

    def _build_output_path(
        self, playlist_name: str, track: TrackInfo, ext: str
    ) -> str:
        safe_playlist = sanitize_filename(playlist_name)
        safe_name = sanitize_filename(f"{track.artist} - {track.title}")
        return os.path.join(
            self.settings.download_dir, safe_playlist, f"{safe_name}{ext}"
        )

    def _get_extension(self, filename: str) -> str:
        lower = filename.lower()
        if lower.endswith(".flac"):
            return ".flac"
        return ".mp3"

    def _find_downloaded_file(
        self, username: str, soulseek_filename: str
    ) -> Optional[str]:
        if not self.settings.slskd_download_dir:
            logger.error("SLSKD_DOWNLOAD_DIR is not set!")
            return None

        base_dir = self.settings.slskd_download_dir
        logger.info(f"SLSKD_DOWNLOAD_DIR={base_dir}, exists={os.path.exists(base_dir)}")

        # Log what's actually in the download dir (top level)
        if os.path.exists(base_dir):
            try:
                top_items = os.listdir(base_dir)
                logger.info(f"Top-level contents of {base_dir}: {top_items[:20]}")
            except Exception as e:
                logger.error(f"Cannot list {base_dir}: {e}")

        # Extract just the filename from the soulseek path
        # e.g. "@@user\\Music\\Artist\\song.mp3" -> "song.mp3"
        parts = soulseek_filename.replace("\\", "/").split("/")
        local_filename = parts[-1] if parts else soulseek_filename
        logger.info(f"Looking for file: '{local_filename}' from user '{username}'")

        # slskd stores downloads in various structures:
        # /downloads/<username>/<remote_path>/file.mp3
        # /downloads/complete/<username>/<remote_path>/file.mp3
        # /downloads/<remote_path>/file.mp3
        # We just walk the entire directory tree to find it

        # Search entire download dir recursively
        for root, dirs, files in os.walk(base_dir):
            for fname in files:
                if fname == local_filename:
                    full_path = os.path.join(root, fname)
                    logger.info(f"Found exact match: {full_path}")
                    return full_path

        # Case-insensitive fallback
        local_lower = local_filename.lower()
        for root, dirs, files in os.walk(base_dir):
            for fname in files:
                if fname.lower() == local_lower:
                    full_path = os.path.join(root, fname)
                    logger.info(f"Found case-insensitive match: {full_path}")
                    return full_path

        # Log everything we found for debugging
        all_files = []
        for root, dirs, files in os.walk(base_dir):
            for fname in files:
                all_files.append(os.path.join(root, fname))
        logger.error(
            f"File '{local_filename}' not found. "
            f"All files in {base_dir}: {all_files[:30]}"
        )
        return None

    def _debug_list_dir(self, path: str, max_depth: int = 3) -> str:
        results = []
        if not os.path.exists(path):
            return f"PATH DOES NOT EXIST: {path}"
        for root, dirs, files in os.walk(path):
            depth = root.replace(path, "").count(os.sep)
            if depth >= max_depth:
                continue
            indent = "  " * depth
            results.append(f"{indent}{os.path.basename(root)}/")
            for f in files[:10]:
                results.append(f"{indent}  {f}")
            if len(files) > 10:
                results.append(f"{indent}  ... and {len(files) - 10} more")
        return "\n".join(results[:50])
