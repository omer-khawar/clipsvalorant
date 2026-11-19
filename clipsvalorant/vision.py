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


class Reader:
    """Wraps the OCR engine, which is loaded once and is slow to start."""

    def __init__(self) -> None:
        self._engine = None
        self._tried = False

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
