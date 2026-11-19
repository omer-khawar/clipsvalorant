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
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PLAYLIST_TIMEOUT_S = 90
FRAME_TIMEOUT_S = 120
# How many frames to have in flight while the caller is reading one. Fetching
# a frame costs about two seconds and OCR about two and a half, and they use
# different resources, so overlapping them hides most of the fetch. Four is
# past the point where more helps: measured at 2.2x on four workers and 2.6x
# on eight, against ffmpeg processes that are not free to spawn.
PREFETCH = 4
# How many VODs to resolve playlists for at once. Resolving one costs about
# 2.6 seconds, of which 1.25 is `yt-dlp` starting Python up before it has
# touched the network at all - so this is idle time twice over, and a scan
# that looks at several broadcasts used to pay it once per broadcast in a
# row. Six at a time is comfortable: they are separate processes waiting on
# Twitch, not work this machine is doing.
PLAYLIST_WORKERS = 6
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
        url = self._resolve(vod_id)
        self._playlists[vod_id] = url
        return url

    def _resolve(self, vod_id: str) -> str | None:
        """Ask yt-dlp for the HLS URL. No caching: the callers do that."""
        try:
            done = subprocess.run(
                ["yt-dlp", "-g", "--no-warnings",
                 "-f", f"bv*[height<={MAX_HEIGHT}]/bv*",
                 f"https://www.twitch.tv/videos/{vod_id}"],
                capture_output=True, text=True, timeout=PLAYLIST_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        for line in done.stdout.splitlines():
            if line.startswith("http"):
                return line.strip()
        return None

    def prefetch_playlists(
        self, vod_ids: Iterable[str], *, workers: int = PLAYLIST_WORKERS
    ) -> None:
        """Resolve several VODs' playlists at once, into the same cache.

        Nothing here returns anything: callers go on using `playlist`, which
        afterwards answers from the cache. It exists because resolving a
        playlist is the longest single wait in a scan and the least
        productive - a subprocess starting up, then a round trip to Twitch -
        and because the VODs a match needs are all known before any of them
        is opened. Done one at a time they stack; done together they cost
        about as much as the slowest one.

        A VOD that fails is cached as a failure exactly as `playlist` would
        cache it, so this never changes what a later call concludes - only
        when it was found out.
        """
        wanted = [v for v in dict.fromkeys(vod_ids) if v and v not in self._playlists]
        if not wanted:
            return
        if len(wanted) == 1:
            self.playlist(wanted[0])
            return
        with ThreadPoolExecutor(max_workers=min(workers, len(wanted))) as pool:
            jobs = {pool.submit(self._resolve, v): v for v in wanted}
            for job, vod_id in jobs.items():
                try:
                    self._playlists[vod_id] = job.result()
                except Exception:
                    # Same answer `playlist` gives when yt-dlp will not talk
                    # to us: there is no playlist, and that is not an error.
                    self._playlists[vod_id] = None

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

    def iter_frames(
        self, vod_id: str, offsets: Iterable[float], *, ahead: int = PREFETCH,
    ) -> Iterator[tuple[float, Path | None]]:
        """Frames in the order asked for, fetched several at a time.

        Fetching and reading a frame cost about the same and compete for
        nothing: one is ffmpeg waiting on Twitch, the other is OCR on the CPU.
        Run serially they add up, which is what made a ten-frame look at a
        broadcast take the better part of a minute.

        Only `ahead` frames are in flight at once, and the rest are never
        submitted, because every caller here stops early - `watch` the moment
        the answer is certain. Fetching all of them up front would pay for
        frames nobody looks at, which on a channel that is obviously the right
        one is most of them.
        """
        offsets = list(offsets)
        if not offsets:
            return
        # The playlist is fetched once, here, rather than raced for by the
        # first `ahead` workers at the same time.
        self.playlist(vod_id)
        pool = ThreadPoolExecutor(max_workers=max(1, ahead))
        pending: dict[int, Future] = {}
        try:
            nxt = 0
            for i in range(min(ahead, len(offsets))):
                pending[i] = pool.submit(self.frame, vod_id, offsets[i])
                nxt = i + 1
            for i in range(len(offsets)):
                future = pending.pop(i)
                try:
                    path = future.result()
                except Exception:  # a frame that will not read is not an error
                    path = None
                if nxt < len(offsets):
                    pending[nxt] = pool.submit(self.frame, vod_id, offsets[nxt])
                    nxt += 1
                yield offsets[i], path
        finally:
            for future in pending.values():
                future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)

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
