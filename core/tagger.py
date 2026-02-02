import io

import httpx
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TRCK, APIC, TBPM, TKEY, TDRC, TXXX, ID3NoHeaderError
from mutagen.flac import FLAC, Picture
from PIL import Image

from core.models import TrackInfo


class Tagger:
    async def tag_file(self, filepath: str, track: TrackInfo) -> None:
        cover_data = await self._fetch_cover_art(track.cover_url)

        if filepath.lower().endswith(".mp3"):
            self._tag_mp3(filepath, track, cover_data)
        elif filepath.lower().endswith(".flac"):
            self._tag_flac(filepath, track, cover_data)

    async def _fetch_cover_art(self, url: str) -> bytes:
        if not url:
            return b""
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            img = Image.open(io.BytesIO(resp.content))
            img = img.resize((600, 600), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue()

    def _tag_mp3(self, filepath: str, track: TrackInfo, cover_data: bytes) -> None:
        audio = MP3(filepath)
        try:
            audio.add_tags()
        except Exception:
            pass

        tags = audio.tags
        if tags is None:
            return

        tags.add(TIT2(encoding=3, text=[track.title]))
        tags.add(TPE1(encoding=3, text=[track.artist]))
        tags.add(TALB(encoding=3, text=[track.album]))
        tags.add(TRCK(encoding=3, text=[f"{track.track_number}/{track.total_tracks}"]))

        # Year
        if track.year:
            tags.add(TDRC(encoding=3, text=[track.year]))

        # BPM
        if track.bpm:
            tags.add(TBPM(encoding=3, text=[str(int(round(track.bpm)))]))

        # Musical key (e.g. "Cm", "F#")
        if track.key:
            tags.add(TKEY(encoding=3, text=[track.key]))

        # Camelot key as custom tag (used by Rekordbox, Traktor, etc.)
        if track.initial_key:
            tags.add(TXXX(encoding=3, desc="INITIAL_KEY", text=[track.initial_key]))

        if cover_data:
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_data,
            ))

        audio.save()

    def _tag_flac(self, filepath: str, track: TrackInfo, cover_data: bytes) -> None:
        audio = FLAC(filepath)
        audio["title"] = track.title
        audio["artist"] = track.artist
        audio["album"] = track.album
        audio["tracknumber"] = str(track.track_number)
        audio["tracktotal"] = str(track.total_tracks)

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()
