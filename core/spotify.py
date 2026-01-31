import re
from urllib.parse import urlparse, parse_qs

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from core.models import TrackInfo


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

            tracks.append(TrackInfo(
                title=t["name"],
                artist=artist_name,
                album=album.get("name", "Unknown Album"),
                track_number=t.get("track_number", 0),
                total_tracks=album.get("total_tracks", 0),
                duration_ms=t.get("duration_ms", 0),
                cover_url=cover_url,
                spotify_uri=t.get("uri", ""),
            ))

        return playlist_name, tracks
