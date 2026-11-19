"""Reading what is on the screen.

The question every other layer dances around is "was this broadcast showing
*our* match?". A name that matches, a title that says valorant and a VOD that
covers the right half hour are all consistent with that -- and equally
consistent with a stranger who happened to be live. Only the pixels settle it.

Valorant puts player names on screen constantly: the killfeed, the combat
report in the corner, the scoreboard, the "switch player" banner while you are
dead and spectating. We know all ten names in the lobby from the match history.
So the test is simply: seek to a moment when something was happening, read the
text, and count how many of those ten names come back.

Measured on the two broadcasts we had already confirmed by other means, this
returns nine names out of ten and seven out of ten. On a broadcast of a
different game, or of a different Valorant match, it returns none. There is no
grey zone to tune a threshold into, which is the nicest property a signal can
have.

OCR is an optional dependency. Without it every other layer still works and
this one reports itself unavailable, rather than the whole tool refusing to
run over a feature most sweeps never reach.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Words Valorant paints on the screen that other games do not. Compared after
# stripping punctuation and spaces, because OCR drops and invents both.
VALORANT_UI_WORDS = (
    "combatreport", "buyphase", "switchplayer", "roundongoing", "killedby",
    "matchpoint", "spikeplanted", "roundwon", "roundlost", "defending",
    "attacking", "eliminated", "pressbtobuy", "toggleshop", "outgoing",
)
# A match timer, top centre: "1:34". Necessary for Valorant, nowhere near
# sufficient -- Deadlock paints one in very nearly the same place.
TIMER_RE = re.compile(r"^\d{1,2}:\d{2}$")
TIMER_BAND_Y = 0.10
TIMER_BAND_X = (0.40, 0.60)

# Below this, a name is too short to mean anything: three characters will turn
# up inside some unrelated word sooner or later.
MIN_NAME_LEN = 4


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


@dataclass(frozen=True)
class TextBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class FrameRead:
    """What one frame turned out to contain."""

    boxes: tuple[TextBox, ...] = ()
    width: int = 0
    height: int = 0

    @property
    def blob(self) -> str:
        return " ".join(normalise(b.text) for b in self.boxes)

    @property
    def has_match_timer(self) -> bool:
        if not self.width or not self.height:
            return False
        for b in self.boxes:
            if not TIMER_RE.match(b.text.strip()):
                continue
            cx = (b.x0 + b.x1) / 2 / self.width
            if b.y1 / self.height <= TIMER_BAND_Y and TIMER_BAND_X[0] <= cx <= TIMER_BAND_X[1]:
                return True
        return False

    @property
    def valorant_ui_words(self) -> tuple[str, ...]:
        blob = self.blob
        return tuple(w for w in VALORANT_UI_WORDS if w in blob)

    @property
    def looks_like_valorant(self) -> bool:
        """Valorant's own furniture is on screen, whoever is playing."""
        return bool(self.valorant_ui_words)

    def names_present(self, names: dict[str, str]) -> set[str]:
        """Which of `names` (normalised -> original) appear in this frame."""
        blob = self.blob
        return {original for key, original in names.items() if key in blob}


# Frames remembered per run. OCR is the expensive half of looking at a
# broadcast - between half a second and five, depending on how much text is on
# screen - and the same frame is routinely asked for twice: once to hunt for
# lobby names and again to read the round countdown off it. A few hundred
# parsed frames cost very little to keep and save seconds each.
OCR_CACHE_MAX = 512

# Frames remembered between runs, as a small JSON file beside each cached
# frame. Reading a frame costs about a second and a quarter of CPU, and it is
# the one cost in a scan that nothing hides: it does not overlap with the
# network, it does not thread (onnxruntime already uses every core), and no
# engine setting makes it cheaper - larger recognition batches measured
# *slower*, because every crop in a batch is padded to the widest one.
#
# What is left is not reading the same frame twice. The frames themselves are
# already kept on disk so that a re-run downloads nothing; this keeps what we
# read off them too, which is what a sweep over the same fortnight spends
# nearly all its time rediscovering. The text of a given frame never changes,
# so this cannot alter a verdict - only how long it takes to reach it.
OCR_SIDECAR_VERSION = 1


class Reader:
    """Wraps the OCR engine, which is loaded once and is slow to start."""

    def __init__(self) -> None:
        self._engine = None
        self._tried = False
        self._cache: dict[str, "FrameRead | None"] = {}

    @property
    def available(self) -> bool:
        self._load()
        return self._engine is not None

    def _load(self) -> None:
        if self._tried:
            return
        self._tried = True
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return
        try:
            self._engine = RapidOCR()
        except Exception:
            self._engine = None

    def read(self, path: Path) -> FrameRead | None:
        self._load()
        if self._engine is None:
            return None
        key = str(path)
        if key in self._cache:
            return self._cache[key]
        out = _load_sidecar(path)
        if out is None:
            out = self._read_uncached(path)
            # Only a real read is written down. A failure here is usually a
            # half-written frame or a busy engine, and caching it on disk
            # would make a passing problem permanent.
            if out is not None:
                _save_sidecar(path, out)
        if len(self._cache) >= OCR_CACHE_MAX:
            # Oldest first; insertion order is close enough to use order here,
            # and the alternative is an LRU for a dictionary that rarely fills.
            for stale in list(self._cache)[: OCR_CACHE_MAX // 4]:
                self._cache.pop(stale, None)
        self._cache[key] = out
        return out

    def _read_uncached(self, path: Path) -> FrameRead | None:
        try:
            from PIL import Image
            with Image.open(path) as im:
                width, height = im.size
            result, _ = self._engine(str(path))
        except Exception:
            return None
        boxes = []
        for box, text, _conf in (result or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            boxes.append(TextBox(text, min(xs), min(ys), max(xs), max(ys)))
        return FrameRead(tuple(boxes), width, height)


def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".ocr.json")


def _load_sidecar(path: Path) -> FrameRead | None:
    """What we read off this frame last time, if we still trust it."""
    note = _sidecar(path)
    try:
        blob = json.loads(note.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("v") != OCR_SIDECAR_VERSION:
        return None
    try:
        boxes = tuple(
            TextBox(str(t), float(x0), float(y0), float(x1), float(y1))
            for t, x0, y0, x1, y1 in blob["boxes"]
        )
        return FrameRead(boxes, int(blob["w"]), int(blob["h"]))
    except (KeyError, TypeError, ValueError):
        return None


def _save_sidecar(path: Path, read: FrameRead) -> None:
    """Best effort, and silent when it fails - this is only ever a shortcut."""
    tmp = _sidecar(path).with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({
            "v": OCR_SIDECAR_VERSION,
            "w": read.width,
            "h": read.height,
            "boxes": [[b.text, b.x0, b.y0, b.x1, b.y1] for b in read.boxes],
        }), encoding="utf-8")
        os.replace(tmp, _sidecar(path))
    except (OSError, TypeError, ValueError):
        try:
            tmp.unlink()
        except OSError:
            pass


def wanted_names(players: list[tuple[str, bool]]) -> dict[str, str]:
    """Normalised -> display name, for every lobby name long enough to trust.

    `players` is (name, is_me). Short names are dropped: a three-letter name
    matches inside half the words on a busy screen.
    """
    out: dict[str, str] = {}
    for name, _is_me in players:
        key = normalise(name)
        if len(key) >= MIN_NAME_LEN:
            out[key] = name
    return out
