"""Map and agent artwork, and the numbers that put a kill on a minimap.

Valorant's own asset service (valorant-api.com) publishes, per map, a top-down
minimap image and four numbers that convert the game's world coordinates into
a point on it. Those numbers are the whole reason the map view is possible:
the match history gives every kill as something like (2773, 1880), which means
nothing until you know that for Abyss you multiply by 8.1e-05 and add 0.5.

Two deliberate choices here.

Everything is fetched once and cached on disk, and the page is served those
copies rather than the upstream URLs. A page that embedded
`media.valorant-api.com/...` directly would tell that CDN the viewer's address
and which maps and agents they play, every time they opened it, for a tool
that otherwise talks to nothing but localhost and Twitch. Caching also means
the page keeps working with no network at all, which matters because the rest
of it already does.

And a miss is never an error. No icon, a map the service has not heard of, no
internet - all of them end as "there is no picture", and the page draws the
name instead. Artwork is decoration; the timestamps are the product.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

API = "https://valorant-api.com/v1"
# Long, because the answer changes when Riot ships a map or an agent - a few
# times a year - and a stale minimap is a far smaller problem than a page
# that stalls on a network call every time it is opened.
CATALOGUE_TTL_S = 14 * 86400
FETCH_TIMEOUT_S = 20
# Riot's art is a few hundred KB at most; anything larger is not what we asked
# for and is not going on disk.
MAX_ASSET_BYTES = 8 * 1024 * 1024

# Asset ids are UUIDs from our own database, but they arrive over HTTP from
# the page, so they are checked rather than trusted: this is what stops a
# crafted id naming a file outside the cache directory.
UUID_RE = re.compile(r"^[0-9a-fA-F-]{8,40}$")

KINDS = {
    "map": ("maps", "displayicon.png"),
    "mapsplash": ("maps", "splash.png"),
    "agent": ("agents", "displayicon.png"),
}


@dataclass
class MapInfo:
    """One map's minimap, and how to land a kill on it."""

    uuid: str
    name: str
    x_multiplier: float = 0.0
    y_multiplier: float = 0.0
    x_scalar: float = 0.0
    y_scalar: float = 0.0
    has_icon: bool = False

    @property
    def usable(self) -> bool:
        """Whether a coordinate can be converted at all.

        A map with zero multipliers would land every kill on the same point,
        which looks like a working feature and is not. Better to draw no dots.
        """
        return bool(self.x_multiplier and self.y_multiplier and self.has_icon)

    def point(self, x: float, y: float) -> tuple[float, float] | None:
        """Game coordinates to a fraction of the minimap, or None.

        Note the axes cross over: the game's y drives the image's x. That is
        Riot's convention, not a transcription slip, and getting it the other
        way round produces a plausible-looking map with every dot wrong.
        """
        if not self.usable:
            return None
        ix = y * self.x_multiplier + self.x_scalar
        iy = x * self.y_multiplier + self.y_scalar
        if not (-0.05 <= ix <= 1.05 and -0.05 <= iy <= 1.05):
            return None  # outside the image: a coordinate we cannot explain
        return (min(max(ix, 0.0), 1.0), min(max(iy, 0.0), 1.0))


@dataclass
class Assets:
    """The on-disk cache, and the one network call that fills it."""

    cache_dir: Path
    _maps: dict[str, MapInfo] = field(default_factory=dict)
    _agents: dict[str, str] = field(default_factory=dict)
    _loaded: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # ---------------------------------------------------------- catalogue

    @property
    def _catalogue_path(self) -> Path:
        return self.cache_dir / "catalogue.json"

    def maps(self) -> dict[str, MapInfo]:
        self._load()
        return self._maps

    def map_for(self, uuid: str | None, name: str | None = None) -> MapInfo | None:
        """A map by uuid, falling back to its name for matches ingested before
        uuids were kept."""
        if not uuid and not name:
            return None
        self._load()
        if uuid and uuid in self._maps:
            return self._maps[uuid]
        if name:
            wanted = name.strip().casefold()
            for info in self._maps.values():
                if info.name.casefold() == wanted:
                    return info
        return None

    def _load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            blob = self._read_catalogue()
            if blob is None:
                blob = self._fetch_catalogue()
            self._loaded = True
            if not blob:
                return
            for entry in blob.get("maps", []):
                info = MapInfo(**entry)
                self._maps[info.uuid] = info
            self._agents = dict(blob.get("agents", {}))

    def _read_catalogue(self) -> dict | None:
        path = self._catalogue_path
        try:
            if time.time() - path.stat().st_mtime > CATALOGUE_TTL_S:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _fetch_catalogue(self) -> dict | None:
        """One call for every map, one for every agent. Failure is survivable."""
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_S) as client:
                maps = client.get(f"{API}/maps").json().get("data") or []
                agents = client.get(
                    f"{API}/agents", params={"isPlayableCharacter": "true"}
                ).json().get("data") or []
        except Exception as exc:                      # network, JSON, anything
            log.info("asset catalogue unavailable (%s); names will stand in", exc)
            return None

        blob = {
            "maps": [
                {
                    "uuid": m.get("uuid") or "",
                    "name": m.get("displayName") or "",
                    "x_multiplier": float(m.get("xMultiplier") or 0.0),
                    "y_multiplier": float(m.get("yMultiplier") or 0.0),
                    "x_scalar": float(m.get("xScalarToAdd") or 0.0),
                    "y_scalar": float(m.get("yScalarToAdd") or 0.0),
                    "has_icon": bool(m.get("displayIcon")),
                }
                for m in maps
                if m.get("uuid")
            ],
            "agents": {
                a["uuid"]: a.get("displayName") or ""
                for a in agents
                if a.get("uuid")
            },
        }
        try:
            self._catalogue_path.write_text(json.dumps(blob), encoding="utf-8")
        except OSError:
            pass
        return blob

    # ------------------------------------------------------------- images

    def image(self, kind: str, uuid: str) -> Path | None:
        """A cached image file, fetching it the first time. None if there is none."""
        if kind not in KINDS or not UUID_RE.match(uuid or ""):
            return None
        folder, leaf = KINDS[kind]
        dest = self.cache_dir / f"{kind}-{uuid}.png"
        try:
            if dest.exists() and dest.stat().st_size > 256:
                return dest
        except OSError:
            return None
        # A miss is remembered too, so a map with no artwork is not re-asked
        # for on every single page load.
        miss = dest.with_suffix(".missing")
        try:
            if miss.exists() and time.time() - miss.stat().st_mtime < CATALOGUE_TTL_S:
                return None
        except OSError:
            pass

        url = f"https://media.valorant-api.com/{folder}/{uuid}/{leaf}"
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_S, follow_redirects=True) as client:
                resp = client.get(url)
            ok = (
                resp.status_code == 200
                and resp.headers.get("content-type", "").startswith("image/")
                and len(resp.content) <= MAX_ASSET_BYTES
            )
            if not ok:
                raise ValueError(f"{resp.status_code} {resp.headers.get('content-type')}")
            tmp = dest.with_suffix(".part")
            tmp.write_bytes(resp.content)
            tmp.replace(dest)
            return dest
        except Exception as exc:
            log.debug("no %s artwork for %s (%s)", kind, uuid, exc)
            try:
                miss.write_bytes(b"")
            except OSError:
                pass
            return None
