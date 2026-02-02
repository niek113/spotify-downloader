from pydantic import BaseModel
from enum import Enum
from typing import Optional


class TrackStatus(str, Enum):
    PENDING = "pending"
    SEARCHING = "searching"
    FOUND = "found"
    DOWNLOADING = "downloading"
    TAGGING = "tagging"
    COMPLETE = "complete"
    FAILED = "failed"
    NOT_FOUND = "not_found"


class TrackInfo(BaseModel):
    title: str
    artist: str
    album: str
    track_number: int
    total_tracks: int
    duration_ms: int
    cover_url: str
    spotify_uri: str
    year: str = ""
    bpm: Optional[float] = None
    key: Optional[str] = None          # e.g. "C", "F#m"
    initial_key: Optional[str] = None  # Camelot notation e.g. "8A", "11B"


class TrackJob(BaseModel):
    track: TrackInfo
    status: TrackStatus = TrackStatus.PENDING
    error: Optional[str] = None
    search_id: Optional[str] = None
    slskd_username: Optional[str] = None
    slskd_filename: Optional[str] = None
    output_path: Optional[str] = None
    progress_pct: float = 0.0


class PlaylistJob(BaseModel):
    job_id: str
    playlist_name: str
    playlist_url: str
    tracks: list[TrackJob] = []
    status: str = "running"
    current_track_index: int = 0
