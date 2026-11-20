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
foolish: `yt-dlp` turns a VOD id into a direct HLS playlist, and `ffmpeg` reads
a single frame out of it.

What this module does NOT do any more is hand ffmpeg the whole playlist and an
`-ss` an hour deep. That worked on the machine it was written on and failed
completely on the deployed one: the same command, the same VOD, the same
offset, took 1.7 seconds here and over two minutes there, writing nothing.

Twitch VOD playlists are `#EXT-X-PLAYLIST-TYPE:EVENT` with no byte ranges, and
whether ffmpeg jumps to the right segment or reads up to it from the beginning
turns out to depend on the build. When it reads up to it, a frame three hours
in means downloading three hours of 720p60 -- several gigabytes, certain to hit
`FRAME_TIMEOUT_S` first, so every frame fails and Layer 5 confirms nothing while
looking exactly like a broadcast that had nothing in it.

So we do the seek ourselves. The playlist is a list of segments and their
durations; adding those up says which segment covers a given offset. We fetch
that one segment, hand ffmpeg a playlist containing only it, and seek the
remaining few seconds inside it. That is one ~4 MB download instead of
several GB, it costs about two seconds wherever it runs, and it does not
depend on the demuxer choosing to be clever.
"""

from __future__ import annotations

import bisect
import logging
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

PLAYLIST_TIMEOUT_S = 90
# One segment is about four megabytes and arrives in roughly two seconds. This
# is the ceiling for a segment that stalls, not a budget to be spent: it used
# to be 120, which was sized for seeking an entire broadcast, and that meant
# every unreadable frame cost two minutes to find out about. With the seek
# done here rather than by the demuxer, anything past this is broken rather
# than slow.
FRAME_TIMEOUT_S = 45
# Reading the playlist itself: tens of kilobytes of text, once per VOD.
INDEX_TIMEOUT_S = 30
# A sane ceiling on that text. A seven-hour VOD indexes to about 55 KB, so
# anything past this is not a playlist we understand.
MAX_INDEX_BYTES = 8 * 1024 * 1024
# How many frames to have in flight while the caller is reading one.
#
# This was four, justified by fetching and reading using "different
# resources - one is ffmpeg waiting on Twitch, the other is OCR on the CPU".
# That stopped being true the day the seek moved after `-i`: a fetch now
# decodes up to ten seconds of 720p60 forward, which is CPU work, and on a
# two-core box four of them compete with the OCR they exist to feed.
#
# Measured on the deployed machine, ten fresh frames fetched and read:
#
#     four in flight   181.9s   (ocr 174.7s, waiting on frames 7.2s)
#     two in flight    162.3s   (ocr 160.1s, waiting 2.2s)
#     one in flight    134.2s   (ocr 132.1s, waiting 2.1s)
#
# One at a time wins by 26% with no parallelism at all, because the frames
# were never the wait - 7.2s of 182s - and every core handed to ffmpeg is a
# core taken from the thing that actually costs something.
#
# On a machine with cores to spare the original reasoning still holds, so
# this scales rather than dropping to one everywhere.
PREFETCH = 1 if (os.cpu_count() or 1) <= 2 else 4
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
# Reading a playlist off disk that points at https segments needs both of
# these. Without the whitelist ffmpeg refuses the URLs; without the extensions
# it refuses the `.mp4` fragments. Neither is needed when the playlist itself
# is remote, which is why neither was here before.
LOCAL_HLS_ARGS = (
    "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
    "-allowed_extensions", "ALL",
)
_MAP_URI_RE = re.compile(r'URI="([^"]*)"')
# Sent when fetching a playlist ourselves. The same string henrik.py uses:
# identifying the tool rather than impersonating a browser.
USER_AGENT = "clipsvalorant/0.1 (personal match-to-VOD tool)"


def missing_tools() -> list[str]:
    """Which external programs we need and cannot find."""
    return [t for t in ("yt-dlp", "ffmpeg") if shutil.which(t) is None]


@dataclass(frozen=True)
class Index:
    """A VOD's segments: where each one starts, and what to fetch for it.

    `init` is the initialisation segment named by `#EXT-X-MAP`. Twitch VODs are
    fragmented MP4, and a fragment taken on its own will not decode without it;
    an older MPEG-TS VOD has none and needs none.
    """

    urls: tuple[str, ...]
    starts: tuple[float, ...]
    durations: tuple[float, ...]
    init: str | None = None

    @property
    def total(self) -> float:
        """How long the broadcast is, by its own reckoning."""
        if not self.urls:
            return 0.0
        return self.starts[-1] + self.durations[-1]

    def covering(self, offset: float) -> tuple[int, float] | None:
        """Which segment holds `offset`, and how far into it to seek.

        None means the offset is not in this broadcast at all. That is a real
        answer and a common one -- a stream that joined late or stopped early
        simply does not hold the moment we wanted -- and it is now free to
        discover, where it used to cost a walk through the entire VOD.
        """
        if offset < 0 or not self.urls:
            return None
        i = bisect.bisect_right(self.starts, offset) - 1
        if i < 0:
            return None
        if offset >= self.starts[i] + self.durations[i]:
            return None
        return i, offset - self.starts[i]


def parse_index(text: str, base_url: str) -> Index | None:
    """Read a media playlist into an `Index`, or None if it is not one.

    `base_url` is the playlist's own URL. Segment URIs in a Twitch playlist are
    bare filenames -- `1017.mp4` -- and mean nothing without it.

    Returns None for a master playlist (the one that lists 1080p60, 720p60 and
    so on), because it has renditions rather than segments. Callers treat that
    as "no index" and fall back.
    """
    urls: list[str] = []
    starts: list[float] = []
    durations: list[float] = []
    init: str | None = None
    elapsed = 0.0
    pending: float | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            found = _MAP_URI_RE.search(line)
            if found:
                init = urllib.parse.urljoin(base_url, found.group(1))
            continue
        if line.startswith("#EXTINF:"):
            try:
                pending = float(line[len("#EXTINF:"):].split(",", 1)[0].strip())
            except ValueError:
                pending = None
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            # A URI with no duration in front of it is a rendition, not a
            # segment: this is a master playlist and not ours to read.
            continue
        urls.append(urllib.parse.urljoin(base_url, line))
        starts.append(elapsed)
        durations.append(pending)
        elapsed += pending
        pending = None

    if not urls:
        return None
    return Index(tuple(urls), tuple(starts), tuple(durations), init)


def one_segment_playlist(index: Index, i: int) -> str:
    """A playlist holding exactly one of `index`'s segments, by absolute URL.

    Declared VOD with an ENDLIST because it is complete by construction, which
    leaves ffmpeg nothing to wonder about and nothing to wait for.
    """
    duration = index.durations[i]
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:6",
        f"#EXT-X-TARGETDURATION:{max(1, int(duration) + 1)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    if index.init:
        lines.append(f'#EXT-X-MAP:URI="{index.init}"')
    lines += [f"#EXTINF:{duration:.3f},", index.urls[i], "#EXT-X-ENDLIST"]
    return "\n".join(lines) + "\n"


@dataclass
class FrameReader:
    """Pulls single frames out of Twitch VODs, caching what it can.

    Playlist URLs are signed and expire, so they are cached for this process
    only. Frames are cached on disk: re-running a resolve should not re-download
    anything, which matters because the whole point of a sweep is to run often.
    """

    cache_dir: Path
    _playlists: dict[str, str | None] = None  # type: ignore[assignment]
    _indexes: dict[str, Index | None] = None  # type: ignore[assignment]
    _index_lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._playlists = {}
        # Parsed once per VOD and then shared by the four threads pulling
        # frames out of it, so the fetch happens under a lock rather than
        # four times over.
        self._indexes = {}
        self._index_lock = threading.Lock()
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

    def index(self, vod_id: str) -> Index | None:
        """The VOD's segment table, fetched and parsed once.

        None if the playlist could not be read or is not a list of segments.
        Cached either way, failures included, so a VOD that cannot be indexed
        is not re-fetched by every frame that wanted it.
        """
        with self._index_lock:
            if vod_id in self._indexes:
                return self._indexes[vod_id]
            url = self.playlist(vod_id)
            parsed = self._fetch_index(url) if url else None
            self._indexes[vod_id] = parsed
            return parsed

    @staticmethod
    def _fetch_index(url: str) -> Index | None:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=INDEX_TIMEOUT_S) as resp:
                raw = resp.read(MAX_INDEX_BYTES)
        except Exception:  # noqa: BLE001 - a playlist we cannot read is not an error
            return None
        return parse_index(raw.decode("utf-8", "replace"), url)

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

        index = self.index(vod_id)
        if index is not None:
            hit = index.covering(at)
            if hit is None:
                # Not a failure: this broadcast does not cover that moment.
                log.debug("VOD %s does not reach %ss (it runs %.0fs)",
                          vod_id, at, index.total)
                return None
            return self._frame_from_segment(index, hit, dest)

        # No index to work from, so fall back to letting ffmpeg seek the whole
        # playlist. This is the slow path the rest of this module exists to
        # avoid; it is here because a playlist we could not parse is still
        # worth one attempt, and on a build that seeks properly it is fine.
        log.debug("No segment index for VOD %s, seeking the playlist itself", vod_id)
        return self._run_ffmpeg(["-ss", str(at), "-i", url], dest)

    def _frame_from_segment(
        self, index: Index, hit: tuple[int, float], dest: Path
    ) -> Path | None:
        """Pull the frame out of the single segment that holds it.

        The seek goes AFTER `-i`, which makes it an output option: ffmpeg
        decodes the segment from its start and throws frames away until the
        timestamp, rather than jumping into the middle of the file.

        That ordering is the whole difference between working and not on
        ffmpeg 5.1, which is what Debian bookworm ships and therefore what
        this runs on. Asked to seek into a lone fragmented-MP4 segment it
        lands mid-NAL and gives up:

            Invalid NAL unit size (-37230577 > 6779)

        -- returning 0 and writing nothing, four times out of four. The only
        offsets that survived were the ones that happened to land exactly on
        a segment boundary and so seeked zero seconds, and a kill timestamp
        almost never does. A newer ffmpeg handles it either way, which is
        exactly why this was invisible until it ran on the server.

        Decoding forward costs 1-5s depending on how deep into the ten
        seconds we land, against 0.5s for the seek that does not work.
        """
        i, remainder = hit
        spec = dest.with_suffix(".m3u8")
        try:
            spec.write_text(one_segment_playlist(index, i), encoding="utf-8")
        except OSError:
            return None
        try:
            return self._run_ffmpeg(
                [*LOCAL_HLS_ARGS, "-i", str(spec), "-ss", f"{remainder:.3f}"], dest
            )
        finally:
            try:
                spec.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _run_ffmpeg(args: list[str], dest: Path) -> Path | None:
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", *args,
                 "-frames:v", "1", "-y", str(dest)],
                capture_output=True, timeout=FRAME_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return FrameReader._keep_if_real(dest)

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
