"""Opening the broadcast itself.

Every layer above this one reasons about a VOD from the outside: its title, the
clips cut from it, the window of wall-clock it happens to cover. All of that is
circumstantial, and circumstantial evidence is how `curry` nearly got reported
as a streamer who had never played with us -- a name that matched and a
broadcast that happened to be running.

This module opens the video and looks. That is the only evidence that settles
the question, and it is what klips.live means when it says it confirms the
streamer from their saved video.

Two external programs do the work, because reimplementing either would be
foolish: `yt-dlp` turns a VOD id into a direct HLS playlist, and `ffmpeg` seeks
that playlist and writes a single frame. Seeking a Twitch DVR playlist is cheap
-- ffmpeg reads the index and fetches only the segment it needs -- so a frame
from eight hours into a broadcast costs about two seconds, not an eight-hour
download.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

PLAYLIST_TIMEOUT_S = 90
FRAME_TIMEOUT_S = 120
# 720p is the sweet spot: the HUD text is legible to OCR and the segments are
# small. Below 480p the killfeed stops being readable at all.
MAX_HEIGHT = 720


def missing_tools() -> list[str]:
    """Which external programs we need and cannot find."""
    return [t for t in ("yt-dlp", "ffmpeg") if shutil.which(t) is None]


@dataclass
class FrameReader:
    """Pulls single frames out of Twitch VODs, caching what it can.

    Playlist URLs are signed and expire, so they are cached for this process
    only. Frames are cached on disk: re-running a resolve should not re-download
    anything, which matters because the whole point of a sweep is to run often.
    """

    cache_dir: Path
    _playlists: dict[str, str | None] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._playlists = {}
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def playlist(self, vod_id: str) -> str | None:
        """The direct HLS URL for a VOD, or None if it is gone or subscriber-only."""
        if vod_id in self._playlists:
            return self._playlists[vod_id]
        url = None
        try:
            done = subprocess.run(
                ["yt-dlp", "-g", "--no-warnings",
                 "-f", f"bv*[height<={MAX_HEIGHT}]/bv*",
                 f"https://www.twitch.tv/videos/{vod_id}"],
                capture_output=True, text=True, timeout=PLAYLIST_TIMEOUT_S,
            )
            for line in done.stdout.splitlines():
                if line.startswith("http"):
                    url = line.strip()
                    break
        except (subprocess.SubprocessError, OSError):
            url = None
        self._playlists[vod_id] = url
        return url

    def frame(self, vod_id: str, offset_s: float) -> Path | None:
        """One frame, `offset_s` into the broadcast. None if it could not be read.

        A VOD that has aged out of Twitch's retention, or that is subscriber
        only, simply has no playlist -- that is a normal answer here, not an
        error, and it is the single commonest outcome across a sweep.
        """
        at = max(0, int(offset_s))
        dest = self.cache_dir / f"{vod_id}_{at}.png"
        try:
            if dest.exists() and dest.stat().st_size > 1024:
                return dest
        except OSError:
            return None
        url = self.playlist(vod_id)
        if not url:
            return None
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(at),
                 "-i", url, "-frames:v", "1", "-y", str(dest)],
                capture_output=True, timeout=FRAME_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return self._keep_if_real(dest)

    @staticmethod
    def _keep_if_real(dest: Path) -> Path | None:
        """The frame if ffmpeg actually wrote one, otherwise None and no file.

        Every filesystem call here is guarded. On Windows the file ffmpeg just
        gave up on can still be held open by it, or by a virus scanner, and the
        delete then raises PermissionError -- which used to escape this method
        and take down an entire sweep over one unreadable frame. Failing to
        read a frame is the single commonest outcome in this whole program and
        must never be an error.
        """
        try:
            if dest.exists() and dest.stat().st_size > 1024:
                return dest
        except OSError:
            return None
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        return None
