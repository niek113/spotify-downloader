import logging
import re
from urllib.parse import urlparse, parse_qs

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from core.models import TrackInfo

logger = logging.getLogger(__name__)

# Spotify key mapping: pitch_class (0-11) → note name
PITCH_CLASS_TO_NOTE = {
    0: "C", 1: "Db", 2: "D", 3: "Eb", 4: "E", 5: "F",
    6: "F#", 7: "G", 8: "Ab", 9: "A", 10: "Bb", 11: "B",
}

# Camelot wheel mapping: (pitch_class, mode) → Camelot code
# mode: 0 = minor, 1 = major
CAMELOT_MAP = {
    (0, 1): "8B",  (0, 0): "5A",   # C maj / C min
    (1, 1): "3B",  (1, 0): "12A",  # Db maj / C# min
    (2, 1): "10B", (2, 0): "7A",   # D maj / D min
    (3, 1): "5B",  (3, 0): "2A",   # Eb maj / Eb min
    (4, 1): "12B", (4, 0): "9A",   # E maj / E min
    (5, 1): "7B",  (5, 0): "4A",   # F maj / F min
    (6, 1): "2B",  (6, 0): "11A",  # F# maj / F# min
    (7, 1): "9B",  (7, 0): "6A",   # G maj / G min
    (8, 1): "4B",  (8, 0): "1A",   # Ab maj / Ab min
    (9, 1): "11B", (9, 0): "8A",   # A maj / A min
    (10, 1): "6B", (10, 0): "3A",  # Bb maj / Bb min
    (11, 1): "1B", (11, 0): "10A", # B maj / B min
}


class SpotifyClient:
    def __init__(self, client_id: str, client_secret: str):
        auth = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        self.sp = spotipy.Spotify(auth_manager=auth)

    def extract_playlist_id(self, url: str) -> str:
        url = url.strip()
        # spotify:playlist:XXXXX
        if url.startswith("spotify:playlist:"):
            return url.split(":")[-1]
        # https://open.spotify.com/playlist/XXXXX?si=...
        parsed = urlparse(url)
        match = re.search(r"/playlist/([a-zA-Z0-9]+)", parsed.path)
        if match:
            return match.group(1)
        raise ValueError(f"Could not extract playlist ID from: {url}")

    def get_playlist_tracks(self, playlist_url: str) -> tuple[str, list[TrackInfo]]:
        playlist_id = self.extract_playlist_id(playlist_url)
        playlist = self.sp.playlist(playlist_id)
        playlist_name = playlist["name"]

        tracks: list[TrackInfo] = []
        results = self.sp.playlist_tracks(playlist_id)
        items = list(results["items"])

        while results["next"]:
            results = self.sp.next(results)
            items.extend(results["items"])

        track_ids = []
        for item in items:
            t = item.get("track")
            if t is None:
                continue
            if t.get("is_local", False):
                continue

            artists = t.get("artists", [])
            artist_name = artists[0]["name"] if artists else "Unknown Artist"

            album = t.get("album", {})
            images = album.get("images", [])
            cover_url = images[0]["url"] if images else ""

            # Extract release year
            release_date = album.get("release_date", "")
            year = release_date[:4] if release_date else ""

            track_info = TrackInfo(
                title=t["name"],
                artist=artist_name,
                album=album.get("name", "Unknown Album"),
                track_number=t.get("track_number", 0),
                total_tracks=album.get("total_tracks", 0),
                duration_ms=t.get("duration_ms", 0),
                cover_url=cover_url,
                spotify_uri=t.get("uri", ""),
                year=year,
            )
            tracks.append(track_info)
            track_id = t.get("id")
            track_ids.append(track_id)

        # Fetch audio features (BPM, key) in batches of 100
        self._enrich_audio_features(tracks, track_ids)

        return playlist_name, tracks

    def _enrich_audio_features(self, tracks: list[TrackInfo], track_ids: list[str]) -> None:
        """Fetch BPM and musical key from Spotify Audio Features API."""
        for i in range(0, len(track_ids), 100):
            batch_ids = track_ids[i:i + 100]
            try:
                features_list = self.sp.audio_features(batch_ids)
            except Exception as e:
                logger.warning(f"Failed to fetch audio features: {e}")
                continue

            if not features_list:
                continue

            for j, features in enumerate(features_list):
                idx = i + j
                if idx >= len(tracks) or features is None:
                    continue

                track = tracks[idx]

                # BPM
                tempo = features.get("tempo")
                if tempo and tempo > 0:
                    track.bpm = round(tempo, 1)

                # Musical key
                key_num = features.get("key")  # 0-11 (C to B), -1 = no key
                mode = features.get("mode")     # 0 = minor, 1 = major

                if key_num is not None and key_num >= 0 and mode is not None:
                    note = PITCH_CLASS_TO_NOTE.get(key_num, "")
                    suffix = "m" if mode == 0 else ""
                    track.key = f"{note}{suffix}"

                    camelot = CAMELOT_MAP.get((key_num, mode))
                    if camelot:
                        track.initial_key = camelot

                logger.debug(
                    f"Audio features for {track.title}: "
                    f"BPM={track.bpm}, Key={track.key}, Camelot={track.initial_key}"
                )
