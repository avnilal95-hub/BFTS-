"""
BFTS Stock Watcher — FruityBlox direct-page parser
=================================================

A Discord stock bot built around https://fruityblox.com/stock.

Features
--------
- /stockalert channel role type:mirage|normal
- /stock type:mirage|normal
- /last-seen type:mirage|normal
- /autolast-seen channel type:mirage|normal
- /stats fruit
- /timepassed
- Polls FruityBlox every 100 seconds (1 minute 40 seconds).
- Uses stock_data.json as its persistent database.
- Posts W and L reactions under each automatic stock alert.
- Pings the configured role only when a Legendary or Mythical fruit is present.
- Uses image URLs discovered from FruityBlox pages when possible.
- Sets activity to: Watching Servers N • Users N

Setup
-----
1. Install dependencies from requirements.txt.
2. Set DISCORD_TOKEN in your cloud host environment variables.
   A local bot_token.txt file is also supported.
3. Invite the bot with applications.commands and bot scopes and permissions:
   Send Messages, Embed Links, Read Message History, Use External Emojis
   (the last permission is only needed if W/L are custom server emojis).
4. Enable the Server Members Intent in the Discord Developer Portal if you
   want accurate member totals.
5. Run: python main.py

Important
---------
FruityBlox is a third-party website and its HTML can change. This program uses
direct HTML parsing, not a private API key. If the site's markup changes, the
parser may need to be adjusted. The bot never claims that an unchanged page is
a new stock rotation; it compares normalized stock snapshots and only sends
alerts after a change has been detected.

The source intentionally keeps configuration in Discord slash commands and
the JSON file, rather than requiring a separate config file full of IDs.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import signal
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import urljoin, urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup
from discord import app_commands
from discord.ext import commands, tasks


# ---------------------------------------------------------------------------
# Application constants
# ---------------------------------------------------------------------------

BOT_NAME = "BFTS Stock Watcher"
STOCK_URL = "https://fruityblox.com/stock"
BASE_URL = "https://fruityblox.com"
DATABASE_PATH = Path(os.getenv("STOCK_DATABASE", "stock_data.json"))
TOKEN_FILE = Path("bot_token.txt")

# The requested cadence is 100 seconds. 100 seconds is one minute and forty
# seconds; it is intentionally not 90 seconds.
POLL_INTERVAL_SECONDS = 100

# A timeout protects the event loop if the website is slow or unavailable.
HTTP_TIMEOUT_SECONDS = 20
HTTP_CONNECT_TIMEOUT_SECONDS = 10
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2

# These are the rarities that can cause an alert role to be mentioned.
PING_RARITIES = {"legendary", "mythical"}

# Blox Fruits stock values as displayed on the source page. The parser prefers
# live prices discovered in the page; this map is a fallback for /stats when
# a price is not included in the stock snapshot.
KNOWN_BELI_PRICES: dict[str, int] = {
    "rocket": 5_000,
    "spin": 7_500,
    "blade": 30_000,
    "spring": 60_000,
    "bomb": 80_000,
    "smoke": 100_000,
    "spike": 180_000,
    "flame": 250_000,
    "falcon": 300_000,
    "eagle": 550_000,
    "ice": 350_000,
    "sand": 420_000,
    "dark": 500_000,
    "diamond": 600_000,
    "light": 650_000,
    "rubber": 750_000,
    "barrier": 800_000,
    "ghost": 940_000,
    "magma": 960_000,
    "quake": 1_000_000,
    "buddha": 1_200_000,
    "love": 1_300_000,
    "creation": 1_400_000,
    "spider": 1_500_000,
    "sound": 1_900_000,
    "phoenix": 1_800_000,
    "portal": 1_900_000,
    "rumble": 2_100_000,
    "pain": 2_300_000,
    "blizzard": 2_400_000,
    "gravity": 2_500_000,
    "mammoth": 2_700_000,
    "t-rex": 2_700_000,
    "dough": 2_800_000,
    "shadow": 2_900_000,
    "venom": 3_000_000,
    "control": 3_200_000,
    "spirit": 3_400_000,
    "dragon": 5_000_000,
    "leopard": 5_000_000,
    "kitsune": 8_000_000,
    "yeti": 5_000_000,
    "gas": 3_200_000,
    "tiger": 5_000_000,
}

# Rarity is not always exposed consistently by the source stock page.
# The mapping is a conservative fallback; live rarity labels take priority.
KNOWN_RARITIES: dict[str, str] = {
    "rocket": "Common",
    "spin": "Common",
    "blade": "Common",
    "spring": "Uncommon",
    "bomb": "Uncommon",
    "smoke": "Uncommon",
    "spike": "Uncommon",
    "flame": "Uncommon",
    "falcon": "Uncommon",
    "eagle": "Legendary",
    "ice": "Uncommon",
    "sand": "Uncommon",
    "dark": "Uncommon",
    "diamond": "Uncommon",
    "light": "Rare",
    "rubber": "Rare",
    "barrier": "Rare",
    "ghost": "Rare",
    "magma": "Rare",
    "quake": "Rare",
    "buddha": "Legendary",
    "love": "Rare",
    "creation": "Legendary",
    "spider": "Legendary",
    "sound": "Legendary",
    "phoenix": "Legendary",
    "portal": "Legendary",
    "rumble": "Legendary",
    "pain": "Legendary",
    "blizzard": "Legendary",
    "gravity": "Mythical",
    "mammoth": "Mythical",
    "t-rex": "Mythical",
    "dough": "Mythical",
    "shadow": "Mythical",
    "venom": "Mythical",
    "control": "Mythical",
    "spirit": "Mythical",
    "dragon": "Mythical",
    "leopard": "Mythical",
    "kitsune": "Mythical",
    "yeti": "Mythical",
    "gas": "Mythical",
    "tiger": "Mythical",
}

# Known names are used only to split stock text if HTML structure is unusual.
# The scraper still accepts unrecognized names when the source provides a card.
KNOWN_FRUITS = sorted(KNOWN_BELI_PRICES, key=len, reverse=True)

# Embed palette and layout.
COLOR_NORMAL = discord.Color.from_rgb(244, 190, 35)
COLOR_MIRAGE = discord.Color.from_rgb(170, 93, 202)
COLOR_INFO = discord.Color.from_rgb(88, 101, 242)
COLOR_ERROR = discord.Color.from_rgb(237, 66, 69)
COLOR_SUCCESS = discord.Color.from_rgb(87, 242, 135)

# Avoid Discord role pings from unexpected user-provided text.
SAFE_ALLOWED_MENTIONS = discord.AllowedMentions(
    everyone=False,
    users=False,
    roles=True,
    replied_user=False,
)


# ---------------------------------------------------------------------------
# Logging and common formatting helpers
# ---------------------------------------------------------------------------

def configure_logging() -> None:
    """Configure concise logs that work well in a cloud console."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


configure_logging()
log = logging.getLogger("bfts-stock")


def utc_now() -> dt.datetime:
    """Return a timezone-aware UTC timestamp."""
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    """Return an ISO-8601 timestamp suitable for JSON storage."""
    return utc_now().isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[dt.datetime]:
    """Parse a stored ISO timestamp without crashing on bad data."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def human_duration(seconds: float) -> str:
    """Format uptime into days, hours, minutes, and seconds."""
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds_part = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds_part}s")
    return " ".join(parts)


def format_money(value: Optional[int]) -> str:
    """Format a Beli price with commas, or show a friendly unknown value."""
    if value is None:
        return "Unknown"
    return f"{value:,}"


def title_case_fruit(name: str) -> str:
    """Normalize a fruit name for human-readable embed text."""
    cleaned = re.sub(r"\s+", " ", name.replace("_", " ").strip())
    return cleaned.title()


def normalize_fruit(name: str) -> str:
    """Normalize a fruit name for stable snapshot comparisons."""
    cleaned = html.unescape(name).lower().strip()
    cleaned = cleaned.replace("fruit", "").replace("_", " ")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def slugify(name: str) -> str:
    """Convert a fruit name into a likely FruityBlox item slug."""
    value = normalize_fruit(name).replace(" ", "-")
    value = value.replace("t-rex", "t-rex")
    return value


def clean_text(value: str) -> str:
    """Collapse HTML whitespace and invisible spacing."""
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def safe_int(value: Any) -> Optional[int]:
    """Parse prices from integers, '1,000,000', or nearby text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if value is None:
        return None
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def stock_fingerprint(fruits: Sequence["Fruit"]) -> str:
    """Create a stable hash that ignores ordering differences in the HTML."""
    payload = [
        {
            "name": normalize_fruit(item.name),
            "price": item.price,
            "robux": item.robux,
            "rarity": item.rarity.lower(),
        }
        for item in sorted(fruits, key=lambda item: normalize_fruit(item.name))
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class Fruit:
    """One stock entry parsed from the FruityBlox page."""
    name: str
    price: Optional[int] = None
    robux: Optional[int] = None
    rarity: str = "Unknown"
    image_url: Optional[str] = None
    item_url: Optional[str] = None
    category: str = "fruit"

    def normalized(self) -> str:
        return normalize_fruit(self.name)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "price": self.price,
            "robux": self.robux,
            "rarity": self.rarity,
            "image_url": self.image_url,
            "item_url": self.item_url,
            "category": self.category,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Fruit":
        return cls(
            name=str(value.get("name", "Unknown")),
            price=safe_int(value.get("price")),
            robux=safe_int(value.get("robux")),
            rarity=str(value.get("rarity", "Unknown")),
            image_url=value.get("image_url"),
            item_url=value.get("item_url"),
            category=str(value.get("category", "fruit")),
        )


@dataclasses.dataclass(slots=True)
class StockSnapshot:
    """One normalized stock reading for a dealer."""
    dealer: str
    fruits: list[Fruit]
    fetched_at: str
    source_url: str = STOCK_URL
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = stock_fingerprint(self.fruits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dealer": self.dealer,
            "fruits": [fruit.as_dict() for fruit in self.fruits],
            "fetched_at": self.fetched_at,
            "source_url": self.source_url,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "StockSnapshot":
        return cls(
            dealer=str(value.get("dealer", "normal")),
            fruits=[
                Fruit.from_dict(item)
                for item in value.get("fruits", [])
                if isinstance(item, dict)
            ],
            fetched_at=str(value.get("fetched_at", "")),
            source_url=str(value.get("source_url", STOCK_URL)),
            fingerprint=str(value.get("fingerprint", "")),
        )


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------

class StockDatabase:
    """
    Small JSON-backed persistence layer.

    Writes are performed atomically using a temporary file and rename. The
    database is deliberately human-readable, so cloud deployments can inspect
    it during troubleshooting. If the host has an ephemeral filesystem, attach
    a persistent volume or the data may reset when the service restarts.
    """

    def __init__(self, path: Path = DATABASE_PATH):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = self._default_data()

    @staticmethod
    def _default_data() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "created_at": iso_now(),
            "updated_at": iso_now(),
            "snapshots": {
                "normal": None,
                "mirage": None,
            },
            "history": {
                "normal": [],
                "mirage": [],
            },
            "last_seen": {
                "normal": {},
                "mirage": {},
            },
            "alerts": {},
            "autolast_seen": {},
            "last_poll": None,
            "poll_errors": 0,
        }

    async def load(self) -> None:
        """Load JSON data or initialize a new database."""
        async with self.lock:
            if not self.path.exists():
                self.data = self._default_data()
                await self._write_unlocked()
                log.info("Created database at %s", self.path.resolve())
                return
            try:
                raw = await asyncio.to_thread(
                    self.path.read_text, encoding="utf-8"
                )
                loaded = json.loads(raw)
                if not isinstance(loaded, dict):
                    raise ValueError("database root must be an object")
                self.data = self._merge_defaults(loaded)
                await self._write_unlocked()
                log.info("Loaded database from %s", self.path.resolve())
            except Exception:
                log.exception("Database could not be loaded; preserving backup")
                backup = self.path.with_suffix(".corrupt.json")
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self.path.replace, backup)
                self.data = self._default_data()
                await self._write_unlocked()

    def _merge_defaults(self, loaded: dict[str, Any]) -> dict[str, Any]:
        """Keep new fields available when upgrading from older JSON files."""
        defaults = self._default_data()
        for key, value in defaults.items():
            if key not in loaded:
                loaded[key] = value
        for group in ("snapshots", "history", "last_seen"):
            if not isinstance(loaded.get(group), dict):
                loaded[group] = defaults[group]
            for dealer in ("normal", "mirage"):
                loaded[group].setdefault(dealer, defaults[group].get(dealer))
        if not isinstance(loaded.get("alerts"), dict):
            loaded["alerts"] = {}
        if not isinstance(loaded.get("autolast_seen"), dict):
            loaded["autolast_seen"] = {}
        return loaded

    async def _write_unlocked(self) -> None:
        """Write the current data atomically; caller must hold self.lock."""
        self.data["updated_at"] = iso_now()
        serialized = json.dumps(
            self.data, indent=2, ensure_ascii=False, sort_keys=True
        )
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        await asyncio.to_thread(
            temp_path.write_text, serialized + "\n", encoding="utf-8"
        )
        await asyncio.to_thread(os.replace, temp_path, self.path)

    async def save(self) -> None:
        async with self.lock:
            await self._write_unlocked()

    async def get_snapshot(self, dealer: str) -> Optional[StockSnapshot]:
        async with self.lock:
            value = self.data["snapshots"].get(dealer)
            if not isinstance(value, dict):
                return None
            return StockSnapshot.from_dict(value)

    async def get_history(self, dealer: str) -> list[dict[str, Any]]:
        async with self.lock:
            values = self.data["history"].get(dealer, [])
            return list(values) if isinstance(values, list) else []

    async def set_snapshot(
        self, dealer: str, snapshot: StockSnapshot, changed: bool
    ) -> None:
        async with self.lock:
            previous = self.data["snapshots"].get(dealer)
            self.data["snapshots"][dealer] = snapshot.as_dict()
            self.data["last_poll"] = iso_now()
            if changed or previous is None:
                history = self.data["history"].setdefault(dealer, [])
                history.append(snapshot.as_dict())
                # Keep the JSON database bounded but useful for /last-seen.
                self.data["history"][dealer] = history[-200:]
                for fruit in snapshot.fruits:
                    key = fruit.normalized()
                    self.data["last_seen"].setdefault(dealer, {})[key] = {
                        **fruit.as_dict(),
                        "seen_at": snapshot.fetched_at,
                        "dealer": dealer,
                    }
            await self._write_unlocked()

    async def increment_poll_error(self) -> None:
        async with self.lock:
            self.data["poll_errors"] = int(self.data.get("poll_errors", 0)) + 1
            await self._write_unlocked()

    async def get_last_seen(self, dealer: str) -> dict[str, Any]:
        async with self.lock:
            value = self.data.get("last_seen", {}).get(dealer, {})
            return dict(value) if isinstance(value, dict) else {}

    async def get_alert_config(self, guild_id: int) -> dict[str, Any]:
        async with self.lock:
            value = self.data["alerts"].get(str(guild_id), {})
            return dict(value) if isinstance(value, dict) else {}

    async def set_alert_config(
        self, guild_id: int, channel_id: int, role_id: int, dealer: str
    ) -> None:
        async with self.lock:
            self.data["alerts"][str(guild_id)] = {
                "channel_id": channel_id,
                "role_id": role_id,
                "dealer": dealer,
                "updated_at": iso_now(),
            }
            await self._write_unlocked()

    async def get_autolast_config(self, guild_id: int) -> dict[str, Any]:
        async with self.lock:
            value = self.data["autolast_seen"].get(str(guild_id), {})
            return dict(value) if isinstance(value, dict) else {}

    async def set_autolast_config(
        self, guild_id: int, channel_id: int, dealer: str
    ) -> None:
        async with self.lock:
            self.data["autolast_seen"][str(guild_id)] = {
                "channel_id": channel_id,
                "dealer": dealer,
                "updated_at": iso_now(),
            }
            await self._write_unlocked()


# ---------------------------------------------------------------------------
# Direct HTML fetching and parsing
# ---------------------------------------------------------------------------

class FruityBloxClient:
    """Fetches the public stock page and extracts stock entries from HTML."""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_success_at: Optional[str] = None
        self.last_error: Optional[str] = None

    async def start(self) -> None:
        if self.session and not self.session.closed:
            return
        timeout = aiohttp.ClientTimeout(
            total=HTTP_TIMEOUT_SECONDS,
            connect=HTTP_CONNECT_TIMEOUT_SECONDS,
        )
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (compatible; BFTSStockWatcher/1.0; "
                "+https://fruityblox.com/stock)"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }
        self.session = aiohttp.ClientSession(timeout=timeout, headers=headers)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def fetch_html(self) -> str:
        """Fetch HTML with a few retries for transient network failures."""
        await self.start()
        assert self.session is not None
        last_exc: Optional[BaseException] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with self.session.get(STOCK_URL) as response:
                    if response.status >= 400:
                        raise RuntimeError(
                            f"FruityBlox returned HTTP {response.status}"
                        )
                    body = await response.text(errors="replace")
                    if not body.strip():
                        raise RuntimeError("FruityBlox returned an empty page")
                    self.last_success_at = iso_now()
                    self.last_error = None
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as exc:
                last_exc = exc
                self.last_error = str(exc)
                log.warning(
                    "Stock page fetch failed (%s/%s): %s",
                    attempt, MAX_RETRIES, exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY_SECONDS * attempt)
        raise RuntimeError(f"Unable to fetch stock page: {last_exc}")

    async def fetch_snapshots(self) -> dict[str, StockSnapshot]:
        """Fetch the page once and parse both dealer sections."""
        body = await self.fetch_html()
        parsed = self.parse_page(body)
        now = iso_now()
        return {
            dealer: StockSnapshot(
                dealer=dealer,
                fruits=parsed.get(dealer, []),
                fetched_at=now,
            )
            for dealer in ("normal", "mirage")
        }

    def parse_page(self, body: str) -> dict[str, list[Fruit]]:
        """
        Parse Normal and Mirage sections.

        First try section-oriented DOM parsing. If that does not find enough
        entries, use link/image metadata and nearby text as a fallback. This
        accommodates minor markup changes without calling any API endpoint.
        """
        soup = BeautifulSoup(body, "html.parser")
        result = {"normal": [], "mirage": []}

        # Remove elements that can introduce irrelevant words into text parsing.
        for node in soup(["script", "style", "noscript", "svg"]):
            node.decompose()

        headings = self._find_dealer_headings(soup)
        for dealer, heading in headings.items():
            section = self._section_for_heading(heading)
            fruits = self._parse_section(section)
            if fruits:
                result[dealer] = self._dedupe_fruits(fruits)

        # Fallback: collect linked item cards and classify from the section text.
        if not result["normal"] or not result["mirage"]:
            fallback = self._parse_by_link_cards(soup)
            page_text = clean_text(soup.get_text(" ", strip=True)).lower()
            for dealer in ("normal", "mirage"):
                if result[dealer]:
                    continue
                marker = "normal" if dealer == "normal" else "mirage"
                if marker in page_text:
                    result[dealer] = fallback.get(dealer, [])

        # Some site versions have no obvious section wrappers. A global parse
        # can still recover the current fruits, but do not falsely duplicate
        # the same list for both dealers.
        if not result["normal"] and not result["mirage"]:
            global_fruits = self._parse_section(soup)
            if global_fruits:
                result["normal"] = self._dedupe_fruits(global_fruits)

        # Log a warning rather than silently treating a parser miss as a stock
        # rotation. The polling loop does not send an empty alert.
        for dealer, fruits in result.items():
            if not fruits:
                log.warning(
                    "Parser did not find %s stock. The source HTML may have changed.",
                    dealer,
                )
        return result

    def _find_dealer_headings(self, soup: BeautifulSoup) -> dict[str, Any]:
        """Find headings whose visible text is Normal or Mirage."""
        found: dict[str, Any] = {}
        for node in soup.find_all(["h1", "h2", "h3", "h4", "strong"]):
            label = normalize_fruit(clean_text(node.get_text(" ", strip=True)))
            if label in {"normal", "normal stock"} and "normal" not in found:
                found["normal"] = node
            elif label in {"mirage", "mirage stock"} and "mirage" not in found:
                found["mirage"] = node
        return found

    def _section_for_heading(self, heading: Any) -> Any:
        """Return the most useful container following a dealer heading."""
        parent = heading
        for _ in range(4):
            if parent is None:
                break
            text_value = clean_text(parent.get_text(" ", strip=True)).lower()
            if "normal" in text_value and "mirage" in text_value:
                parent = parent.parent
                continue
            links = parent.find_all("a") if hasattr(parent, "find_all") else []
            if len(links) >= 1:
                return parent
            parent = parent.parent
        # Walk forward siblings until another dealer heading.
        collected = []
        sibling = heading
        for _ in range(40):
            sibling = sibling.find_next()
            if sibling is None:
                break
            if getattr(sibling, "name", None) in {"h2", "h3"}:
                label = normalize_fruit(clean_text(sibling.get_text(" ", strip=True)))
                if label in {"normal", "mirage"}:
                    break
            if getattr(sibling, "name", None) in {"a", "article", "li", "div"}:
                collected.append(sibling)
        return heading.parent if not collected else collected[0].parent

    def _parse_section(self, section: Any) -> list[Fruit]:
        """Extract fruits from a DOM section with item links, images, and text."""
        if section is None:
            return []
        candidates: list[Fruit] = []
        seen_urls: set[str] = set()
        anchors = section.find_all("a", href=True) if hasattr(section, "find_all") else []
        for anchor in anchors:
            href = str(anchor.get("href", ""))
            text_value = clean_text(anchor.get_text(" ", strip=True))
            image = anchor.find("img")
            image_url = self._image_url(image) if image else None
            title = (
                clean_text(image.get("alt", "")) if image else ""
            ) or clean_text(anchor.get("title", "")) or text_value
            fruit = self._fruit_from_text(title, text_value, href, image_url)
            if fruit is None:
                continue
            identity = normalize_fruit(fruit.name)
            if identity in seen_urls:
                continue
            seen_urls.add(identity)
            candidates.append(fruit)

        # Some layouts put item cards in divs instead of anchors.
        if not candidates:
            for card in section.find_all(["article", "li", "div"]):
                text_value = clean_text(card.get_text(" ", strip=True))
                if not text_value or len(text_value) > 220:
                    continue
                image = card.find("img")
                image_url = self._image_url(image) if image else None
                title = (
                    clean_text(image.get("alt", "")) if image else ""
                ) or clean_text(card.get("aria-label", "")) or text_value
                fruit = self._fruit_from_text(title, text_value, "", image_url)
                if fruit:
                    candidates.append(fruit)
        return self._dedupe_fruits(candidates)

    def _parse_by_link_cards(self, soup: BeautifulSoup) -> dict[str, list[Fruit]]:
        """Fallback that associates stock cards with the nearest dealer section."""
        result: dict[str, list[Fruit]] = {"normal": [], "mirage": []}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", ""))
            if "/items/" not in href:
                continue
            text_value = clean_text(anchor.get_text(" ", strip=True))
            image = anchor.find("img")
            image_url = self._image_url(image) if image else None
            title = (
                clean_text(image.get("alt", "")) if image else ""
            ) or clean_text(anchor.get("title", "")) or text_value
            fruit = self._fruit_from_text(title, text_value, href, image_url)
            if not fruit:
                continue
            ancestor = anchor
            nearby = ""
            for _ in range(5):
                if ancestor is None:
                    break
                nearby = clean_text(ancestor.get_text(" ", strip=True)).lower()
                if "normal" in nearby or "mirage" in nearby:
                    break
                ancestor = ancestor.parent
            dealer = "mirage" if "mirage" in nearby else "normal"
            result[dealer].append(fruit)
        return {key: self._dedupe_fruits(value) for key, value in result.items()}

    def _image_url(self, image: Any) -> Optional[str]:
        """Return a valid absolute image URL from an HTML image element."""
        if image is None:
            return None
        for attribute in ("src", "data-src", "data-lazy-src", "srcset"):
            raw = image.get(attribute)
            if not raw:
                continue
            if attribute == "srcset":
                raw = str(raw).split(",")[0].strip().split(" ")[0]
            value = urljoin(BASE_URL, str(raw))
            if value.startswith("https://") or value.startswith("http://"):
                return value
        return None

    def _fruit_from_text(
        self,
        title: str,
        nearby_text: str,
        href: str,
        image_url: Optional[str],
    ) -> Optional[Fruit]:
        """Turn one link/card into a Fruit if it resembles a stock item."""
        combined = clean_text(f"{title} {nearby_text}")
        if not combined:
            return None
        lower = combined.lower()
        if any(word in lower for word in (
            "sign in", "privacy policy", "terms of service", "fruityblox.com logo",
            "next reset", "stock right now", "learn more",
        )):
            return None

        name = self._extract_name(title, href, nearby_text)
        if not name:
            return None
        normalized = normalize_fruit(name)
        if normalized in {"normal", "mirage", "stock", "fruit"}:
            return None

        price = self._extract_price(combined, normalized)
        robux = self._extract_robux(combined)
        rarity = self._extract_rarity(combined, normalized)
        item_url = urljoin(BASE_URL, href) if href and href.startswith("/") else (
            href if href.startswith("http") else None
        )

        # A candidate without any fruit identity is not useful. A missing price
        # is permitted because /stats can still show its rarity and image.
        return Fruit(
            name=title_case_fruit(name),
            price=price,
            robux=robux,
            rarity=rarity,
            image_url=image_url,
            item_url=item_url,
        )

    def _extract_name(self, title: str, href: str, nearby_text: str) -> Optional[str]:
        """Prefer the item slug or image alt, then match a known fruit name."""
        title = clean_text(title)
        href_slug = ""
        parsed = urlparse(urljoin(BASE_URL, href)) if href else None
        if parsed:
            match = re.search(r"/items/([^/?#]+)", parsed.path)
            if match:
                href_slug = match.group(1).replace("-", " ")
        if href_slug and normalize_fruit(href_slug) not in {"fruit", "stock"}:
            return href_slug

        title_norm = normalize_fruit(title)
        # Remove common words and numeric price fragments from title strings.
        for known in KNOWN_FRUITS:
            if re.search(rf"\b{re.escape(known)}\b", title_norm):
                return known
        if title_norm and len(title_norm) <= 24 and not re.search(r"\d", title_norm):
            if title_norm not in {"normal", "mirage", "stock", "blox fruits"}:
                return title_norm
        nearby_norm = normalize_fruit(nearby_text)
        for known in KNOWN_FRUITS:
            if re.search(rf"\b{re.escape(known)}\b", nearby_norm):
                return known
        return None

    def _extract_price(self, text_value: str, normalized_name: str) -> Optional[int]:
        """Extract a Beli price; fallback to known values for common fruits."""
        # Prices in the stock page are normally displayed as 5,000 or 2,300,000.
        values = [
            int(match.replace(",", ""))
            for match in re.findall(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b", text_value)
            if match.replace(",", "").isdigit()
        ]
        values = [value for value in values if value not in range(1, 10_000)]
        if values:
            return values[0]
        return KNOWN_BELI_PRICES.get(normalized_name)

    def _extract_robux(self, text_value: str) -> Optional[int]:
        """Extract Robux amount where the source page displays one."""
        match = re.search(r"\b(\d[\d,]*)\s*(?:R\b|robux\b)", text_value, re.I)
        return safe_int(match.group(1)) if match else None

    def _extract_rarity(self, text_value: str, normalized_name: str) -> str:
        """Read an explicit rarity label or use a known fallback."""
        match = re.search(
            r"\b(common|uncommon|rare|legendary|mythical|mythic)\b",
            text_value,
            re.I,
        )
        if match:
            value = match.group(1).title()
            return "Mythical" if value.lower() == "mythic" else value
        return KNOWN_RARITIES.get(normalized_name, "Unknown")

    @staticmethod
    def _dedupe_fruits(fruits: Iterable[Fruit]) -> list[Fruit]:
        """Keep the first occurrence of each fruit while preserving page order."""
        result: list[Fruit] = []
        seen: set[str] = set()
        for fruit in fruits:
            key = fruit.normalized()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(fruit)
        return result


# ---------------------------------------------------------------------------
# Discord embeds and presentation
# ---------------------------------------------------------------------------

RARITY_EMOJI = {
    "Common": "⚪",
    "Uncommon": "🟢",
    "Rare": "🔵",
    "Legendary": "🟡",
    "Mythical": "🟣",
    "Unknown": "⚪",
}


def dealer_title(dealer: str) -> str:
    return "Mirage Stock" if dealer == "mirage" else "Normal Stock"


def dealer_color(dealer: str) -> discord.Color:
    return COLOR_MIRAGE if dealer == "mirage" else COLOR_NORMAL


def dealer_rotation_text(dealer: str) -> str:
    return "🔄 Rotates every 2 hours" if dealer == "mirage" else "🔄 Rotates every 4 hours"


def rarity_emoji(rarity: str) -> str:
    return RARITY_EMOJI.get(rarity.title(), "⚪")


def fruit_line(fruit: Fruit) -> str:
    """Render a spacious line similar to the screenshot without links/buttons."""
    emoji = rarity_emoji(fruit.rarity)
    price = format_money(fruit.price)
    return f"{emoji}  💲 `{price:>10}`  •  **{title_case_fruit(fruit.name)}**"


def build_stock_embed(
    snapshot: StockSnapshot,
    role: Optional[discord.Role] = None,
    alert: bool = False,
) -> discord.Embed:
    """Build the stock embed with blank lines and no trade/shop links."""
    dealer = snapshot.dealer
    embed = discord.Embed(
        title=f"✨ {dealer_title(dealer)}",
        description=(
            f"{dealer_rotation_text(dealer)}\n\n"
            + "\n".join(fruit_line(fruit) for fruit in snapshot.fruits)
        ),
        color=dealer_color(dealer),
        timestamp=parse_iso(snapshot.fetched_at) or utc_now(),
    )
    embed.set_footer(text=f"Updated • {snapshot.fetched_at.replace('T', ' ').replace('+00:00', ' UTC')}")
    if role and alert:
        embed.add_field(name="\u200b", value=f"{role.mention}", inline=False)
    return embed


def build_last_seen_embed(fruit: Fruit, seen_at: str, dealer: str) -> discord.Embed:
    """Build one fruit's last-seen embed with source image if available."""
    embed = discord.Embed(
        title=f"{title_case_fruit(fruit.name)} — {format_money(fruit.price)} Beli",
        description=(
            f"**Dealer:** {dealer_title(dealer)}\n\n"
            f"**Rarity:** {rarity_emoji(fruit.rarity)} {fruit.rarity}\n\n"
            f"**Last seen:** <t:{int((parse_iso(seen_at) or utc_now()).timestamp())}:F>"
        ),
        color=dealer_color(dealer),
        timestamp=parse_iso(seen_at) or utc_now(),
    )
    if fruit.image_url:
        embed.set_thumbnail(url=fruit.image_url)
    embed.set_footer(text="FruityBlox stock history")
    return embed


def build_stats_embed(fruit: Fruit) -> discord.Embed:
    """Build a stats embed with a right-side fruit thumbnail."""
    embed = discord.Embed(
        title=f"{rarity_emoji(fruit.rarity)} {title_case_fruit(fruit.name)}",
        description=(
            f"**Beli price**\n💲 `{format_money(fruit.price)}`\n\n"
            f"**Robux price**\n🪙 `{format_money(fruit.robux)}`\n\n"
            f"**Rarity**\n{fruit.rarity}\n\n"
            f"**Category**\n{fruit.category.title()}"
        ),
        color=COLOR_INFO,
        timestamp=utc_now(),
    )
    if fruit.image_url:
        embed.set_thumbnail(url=fruit.image_url)
    embed.set_footer(text="Values and images are parsed from public FruityBlox pages where available")
    return embed


def build_info_embed(title: str, description: str, color: discord.Color = COLOR_INFO) -> discord.Embed:
    """Shared compact embed builder for informational command responses."""
    embed = discord.Embed(title=title, description=description, color=color, timestamp=utc_now())
    embed.set_footer(text=BOT_NAME)
    return embed


def find_fruit(snapshot: Optional[StockSnapshot], query: str) -> Optional[Fruit]:
    """Find a fruit in a current snapshot using case-insensitive partial matching."""
    if snapshot is None:
        return None
    normalized_query = normalize_fruit(query)
    if not normalized_query:
        return None
    for fruit in snapshot.fruits:
        if fruit.normalized() == normalized_query:
            return fruit
    for fruit in snapshot.fruits:
        if normalized_query in fruit.normalized():
            return fruit
    return None


# ---------------------------------------------------------------------------
# Bot class and application command wiring
# ---------------------------------------------------------------------------

class BFTSStockBot(commands.Bot):
    """Discord bot with a background stock poller and slash commands."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=SAFE_ALLOWED_MENTIONS,
        )
        self.database = StockDatabase()
        self.source = FruityBloxClient()
        self.started_at = time.monotonic()
        self._poll_lock = asyncio.Lock()
        self._initial_sync_done = False

    async def setup_hook(self) -> None:
        await self.database.load()
        await self.source.start()
        # Sync global slash commands. Discord may take a little while to display
        # global commands the first time. Set TEST_GUILD_ID to sync instantly
        # during testing, then remove it for a global deployment.
        test_guild = os.getenv("TEST_GUILD_ID")
        if test_guild and test_guild.isdigit():
            guild_obj = discord.Object(id=int(test_guild))
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
            log.info("Synced slash commands to test guild %s", test_guild)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands")
        self._initial_sync_done = True
        if not stock_poll_loop.is_running():
            stock_poll_loop.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        await self.refresh_presence()
        # Ensure a first reading is available shortly after startup.
        asyncio.create_task(self.poll_once(initial=True))

    async def close(self) -> None:
        if stock_poll_loop.is_running():
            stock_poll_loop.cancel()
        await self.source.close()
        await super().close()

    async def refresh_presence(self) -> None:
        """Set the exact requested Watching activity."""
        guild_count = len(self.guilds)
        user_count = sum(guild.member_count or 0 for guild in self.guilds)
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"Servers {guild_count} • Users {user_count}",
        )
        with contextlib.suppress(discord.HTTPException, RuntimeError):
            await self.change_presence(activity=activity)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.refresh_presence()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await self.refresh_presence()

    async def on_member_join(self, member: discord.Member) -> None:
        # Updating the presence on every join keeps the Users counter current.
        await self.refresh_presence()

    async def poll_once(self, initial: bool = False) -> None:
        """Fetch both stock sections and announce only genuine snapshot changes."""
        if self._poll_lock.locked():
            return
        async with self._poll_lock:
            try:
                snapshots = await self.source.fetch_snapshots()
            except Exception:
                log.exception("Stock poll failed")
                await self.database.increment_poll_error()
                return

            for dealer, snapshot in snapshots.items():
                # An empty parse means a likely page markup change or temporary
                # source problem. Never overwrite a valid snapshot with empty data.
                if not snapshot.fruits:
                    log.warning("Skipping empty %s snapshot", dealer)
                    continue
                previous = await self.database.get_snapshot(dealer)
                changed = previous is not None and previous.fingerprint != snapshot.fingerprint
                first_read = previous is None

                await self.database.set_snapshot(dealer, snapshot, changed=changed)

                if changed:
                    log.info("%s stock changed; announcing new snapshot", dealer)
                    await self.announce_stock_change(snapshot)
                    await self.announce_autolast_seen(snapshot)
                elif first_read:
                    log.info("Saved initial %s stock; no initial alert sent", dealer)
                else:
                    log.debug("%s stock unchanged", dealer)

            await self.refresh_presence()

    async def announce_stock_change(self, snapshot: StockSnapshot) -> None:
        """Send stock embed to the configured channel and add W/L reactions."""
        for guild in list(self.guilds):
            config = await self.database.get_alert_config(guild.id)
            if not config or config.get("dealer") != snapshot.dealer:
                continue
            channel = guild.get_channel(int(config.get("channel_id", 0)))
            role = guild.get_role(int(config.get("role_id", 0)))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                log.warning("Configured alert channel is missing in guild %s", guild.id)
                continue
            mentions = [
                fruit for fruit in snapshot.fruits
                if fruit.rarity.lower() in PING_RARITIES
            ]
            embed = build_stock_embed(snapshot)
            content = role.mention if role and mentions else None
            try:
                message = await channel.send(
                    content=content,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(
                        roles=bool(content),
                        users=False,
                        everyone=False,
                    ),
                )
                # The user requested two reaction emojis: W and L.
                await message.add_reaction("🆆")
                await message.add_reaction("🅻")
            except discord.Forbidden:
                log.warning("Missing permission to send alert in channel %s", channel.id)
            except discord.HTTPException:
                log.exception("Failed to send stock alert in channel %s", channel.id)

    async def announce_autolast_seen(self, snapshot: StockSnapshot) -> None:
        """Publish every fruit's latest seen snapshot when the dealer changes."""
        for guild in list(self.guilds):
            config = await self.database.get_autolast_config(guild.id)
            if not config or config.get("dealer") != snapshot.dealer:
                continue
            channel = guild.get_channel(int(config.get("channel_id", 0)))
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                continue
            try:
                # One message per fruit keeps each fruit's image and timestamp
                # visible, matching the requested /last-seen embed style.
                for fruit in snapshot.fruits:
                    embed = build_last_seen_embed(fruit, snapshot.fetched_at, snapshot.dealer)
                    await channel.send(embed=embed)
            except discord.Forbidden:
                log.warning("Missing permission for autolast-seen in guild %s", guild.id)
            except discord.HTTPException:
                log.exception("Autolast-seen delivery failed for guild %s", guild.id)

    async def current_snapshot(self, dealer: str) -> Optional[StockSnapshot]:
        """Return cached stock; fetch now only if no cached snapshot exists."""
        snapshot = await self.database.get_snapshot(dealer)
        if snapshot is not None and snapshot.fruits:
            return snapshot
        await self.poll_once(initial=True)
        return await self.database.get_snapshot(dealer)

    async def get_last_seen_fruit(self, dealer: str, query: str) -> Optional[dict[str, Any]]:
        records = await self.database.get_last_seen(dealer)
        normalized_query = normalize_fruit(query)
        if normalized_query in records:
            return records[normalized_query]
        for key, value in records.items():
            if normalized_query and normalized_query in key:
                return value
        return None


bot = BFTSStockBot()


# ---------------------------------------------------------------------------
# Slash command helpers
# ---------------------------------------------------------------------------

DealerChoice = app_commands.Choice[str]


async def respond_error(interaction: discord.Interaction, message: str) -> None:
    """Send a consistent ephemeral error response."""
    embed = build_info_embed("⚠️ Could not complete command", message, COLOR_ERROR)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def require_guild(interaction: discord.Interaction) -> bool:
    """Return False and respond if a command is used in DMs."""
    if interaction.guild is None:
        await respond_error(interaction, "This command must be used inside a server.")
        return False
    return True


async def require_admin(interaction: discord.Interaction) -> bool:
    """Require Discord Administrator permission for configuration commands."""
    if not interaction.guild:
        await respond_error(interaction, "Use this command inside a server.")
        return False
    permissions = interaction.user.guild_permissions if isinstance(
        interaction.user, discord.Member
    ) else None
    if permissions is None or not permissions.administrator:
        await respond_error(
            interaction,
            "You need the **Administrator** permission to configure stock channels.",
        )
        return False
    return True


def parse_dealer(value: str) -> str:
    """Normalize a command dealer selection."""
    return "mirage" if value.lower() == "mirage" else "normal"


async def send_stock_response(
    interaction: discord.Interaction,
    dealer: str,
    ephemeral: bool = False,
) -> None:
    """Send the currently cached stock without subscribing the channel."""
    await interaction.response.defer(ephemeral=ephemeral, thinking=True)
    snapshot = await bot.current_snapshot(dealer)
    if not snapshot or not snapshot.fruits:
        await interaction.followup.send(
            embed=build_info_embed(
                "Stock unavailable",
                "I couldn't parse the stock page right now. Try again in a moment.",
                COLOR_ERROR,
            ),
            ephemeral=True,
        )
        return
    await interaction.followup.send(embed=build_stock_embed(snapshot), ephemeral=ephemeral)


# ---------------------------------------------------------------------------
# /stockalert
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="stockalert",
    description="Configure automatic stock-change alerts for this server.",
)
@app_commands.describe(
    channel="Channel where stock changes should be posted",
    role="Role to ping when Legendary or Mythical fruits appear",
    type="Choose Normal or Mirage stock",
)
@app_commands.choices(type=[
    app_commands.Choice(name="Mirage", value="mirage"),
    app_commands.Choice(name="Normal", value="normal"),
])
async def stockalert(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
    type: DealerChoice,
) -> None:
    """Set up one automatic stock alert destination per server."""
    if not await require_admin(interaction):
        return
    if interaction.guild is None:
        return
    if role.is_default:
        await respond_error(interaction, "Please choose a role other than @everyone.")
        return
    if role.managed:
        await respond_error(interaction, "That role is managed by an integration.")
        return
    if interaction.guild.me and role >= interaction.guild.me.top_role:
        await respond_error(
            interaction,
            "Move the bot's highest role above the alert role so it can mention it.",
        )
        return
    dealer = parse_dealer(type.value)
    await bot.database.set_alert_config(
        interaction.guild.id, channel.id, role.id, dealer
    )
    embed = build_info_embed(
        "✅ Stock alerts configured",
        (
            f"**Channel:** {channel.mention}\n\n"
            f"**Role:** {role.mention}\n\n"
            f"**Stock type:** {dealer_title(dealer)}\n\n"
            "I'll check FruityBlox every **100 seconds** and post only when the "
            "stock snapshot changes. The role is mentioned when Legendary or "
            "Mythical fruit is present. Every alert receives W and L reactions."
        ),
        COLOR_SUCCESS,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /stock
# ---------------------------------------------------------------------------

@bot.tree.command(name="stock", description="Show the current Normal or Mirage stock.")
@app_commands.describe(type="Choose Normal or Mirage stock")
@app_commands.choices(type=[
    app_commands.Choice(name="Mirage", value="mirage"),
    app_commands.Choice(name="Normal", value="normal"),
])
async def stock(interaction: discord.Interaction, type: DealerChoice) -> None:
    """Display stock on demand; it does not enable auto-alerts."""
    await send_stock_response(interaction, parse_dealer(type.value), ephemeral=False)


# ---------------------------------------------------------------------------
# /last-seen
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="last-seen",
    description="Show when a fruit was last recorded in stock.",
)
@app_commands.describe(
    fruit="Fruit name to look up",
    type="Choose Normal or Mirage stock",
)
@app_commands.choices(type=[
    app_commands.Choice(name="Mirage", value="mirage"),
    app_commands.Choice(name="Normal", value="normal"),
])
async def last_seen(
    interaction: discord.Interaction,
    fruit: str,
    type: DealerChoice,
) -> None:
    """Look up a fruit's last recorded appearance in stock history."""
    dealer = parse_dealer(type.value)
    await interaction.response.defer(thinking=True, ephemeral=True)
    record = await bot.get_last_seen_fruit(dealer, fruit)
    if not record:
        snapshot = await bot.current_snapshot(dealer)
        current = find_fruit(snapshot, fruit)
        if current and snapshot:
            embed = build_last_seen_embed(current, snapshot.fetched_at, dealer)
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await interaction.followup.send(
            embed=build_info_embed(
                "No last-seen record",
                f"I don't have a saved appearance for **{title_case_fruit(fruit)}** "
                f"in {dealer_title(dealer)} yet.",
                COLOR_ERROR,
            ),
            ephemeral=True,
        )
        return
    item = Fruit.from_dict(record)
    seen_at = str(record.get("seen_at", ""))
    await interaction.followup.send(
        embed=build_last_seen_embed(item, seen_at, dealer),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /autolast-seen
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="autolast-seen",
    description="Post last-seen fruit embeds whenever this stock changes.",
)
@app_commands.describe(
    channel="Channel for automatic last-seen fruit entries",
    type="Choose Normal or Mirage stock",
)
@app_commands.choices(type=[
    app_commands.Choice(name="Mirage", value="mirage"),
    app_commands.Choice(name="Normal", value="normal"),
])
async def autolast_seen(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    type: DealerChoice,
) -> None:
    """Configure automatic last-seen posts, administrator-only."""
    if not await require_admin(interaction):
        return
    if interaction.guild is None:
        return
    dealer = parse_dealer(type.value)
    await bot.database.set_autolast_config(interaction.guild.id, channel.id, dealer)
    await interaction.response.send_message(
        embed=build_info_embed(
            "✅ Auto last-seen enabled",
            f"New **{dealer_title(dealer)}** snapshots will be posted in {channel.mention} "
            "as fruit-by-fruit embeds whenever the stock changes.",
            COLOR_SUCCESS,
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

@bot.tree.command(name="stats", description="Show a fruit's price, rarity, and image.")
@app_commands.describe(fruit="Fruit name to inspect")
async def stats(interaction: discord.Interaction, fruit: str) -> None:
    """Show a fruit's best available parsed metadata and image."""
    await interaction.response.defer(thinking=True, ephemeral=True)
    found: Optional[Fruit] = None
    for dealer in ("normal", "mirage"):
        snapshot = await bot.current_snapshot(dealer)
        found = find_fruit(snapshot, fruit)
        if found:
            break

    # If the fruit is not in current stock, use the saved last-seen database.
    if not found:
        for dealer in ("normal", "mirage"):
            record = await bot.get_last_seen_fruit(dealer, fruit)
            if record:
                found = Fruit.from_dict(record)
                break

    # The stock page may not expose all fruit pages in a single response.
    # Build a best-effort known entry, and try its public item page for image.
    if not found:
        normalized = normalize_fruit(fruit)
        if normalized in KNOWN_BELI_PRICES:
            found = Fruit(
                name=title_case_fruit(normalized),
                price=KNOWN_BELI_PRICES[normalized],
                rarity=KNOWN_RARITIES.get(normalized, "Unknown"),
                item_url=f"{BASE_URL}/items/{slugify(normalized)}",
            )
            found.image_url = await fetch_item_image(found.item_url)

    if not found:
        await interaction.followup.send(
            embed=build_info_embed(
                "Fruit not found",
                f"I couldn't find **{title_case_fruit(fruit)}**. Check the spelling and try again.",
                COLOR_ERROR,
            ),
            ephemeral=True,
        )
        return

    if not found.image_url and found.item_url:
        found.image_url = await fetch_item_image(found.item_url)
    await interaction.followup.send(embed=build_stats_embed(found), ephemeral=True)


async def fetch_item_image(item_url: Optional[str]) -> Optional[str]:
    """Fetch a fruit item page and read og:image/twitter:image metadata."""
    if not item_url:
        return None
    try:
        await bot.source.start()
        assert bot.source.session is not None
        async with bot.source.session.get(item_url) as response:
            if response.status >= 400:
                return None
            body = await response.text(errors="replace")
        soup = BeautifulSoup(body, "html.parser")
        for selector in (
            {"property": "og:image"},
            {"name": "twitter:image"},
            {"property": "og:image:secure_url"},
        ):
            tag = soup.find("meta", attrs=selector)
            if tag and tag.get("content"):
                value = urljoin(BASE_URL, str(tag["content"]))
                if value.startswith("https://") or value.startswith("http://"):
                    return value
        image = soup.find("img", alt=re.compile("fruit", re.I))
        if image:
            return bot.source._image_url(image)
    except Exception:
        log.debug("Could not fetch item image for %s", item_url, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# /timepassed
# ---------------------------------------------------------------------------

@bot.tree.command(name="timepassed", description="See how long the bot has been online.")
async def timepassed(interaction: discord.Interaction) -> None:
    """Report process uptime and bot startup time."""
    elapsed = time.monotonic() - bot.started_at
    started = utc_now() - dt.timedelta(seconds=elapsed)
    embed = build_info_embed(
        "⏱️ Bot uptime",
        (
            f"**Online for**\n{human_duration(elapsed)}\n\n"
            f"**Started**\n<t:{int(started.timestamp())}:F>\n\n"
            f"**Servers**\n{len(bot.guilds):,}\n\n"
            f"**Users**\n{sum(g.member_count or 0 for g in bot.guilds):,}"
        ),
        COLOR_SUCCESS,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Background polling and lifecycle
# ---------------------------------------------------------------------------

@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def stock_poll_loop() -> None:
    """Run the stock checker at the configured interval."""
    await bot.poll_once()


@stock_poll_loop.before_loop
async def before_stock_poll_loop() -> None:
    """Wait until Discord is connected before the first scheduled iteration."""
    await bot.wait_until_ready()


@stock_poll_loop.error
async def stock_poll_loop_error(error: BaseException) -> None:
    """Keep an unexpected loop exception visible in logs."""
    log.exception("Stock polling loop stopped due to an unexpected error", exc_info=error)


# ---------------------------------------------------------------------------
# Diagnostics and graceful shutdown
# ---------------------------------------------------------------------------

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Prevent user-facing slash command errors from becoming silent failures."""
    original = getattr(error, "original", error)
    log.error("Slash command error: %s", original, exc_info=original)
    if isinstance(error, app_commands.MissingPermissions):
        message = "You don't have permission to use this command."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = "Please wait a moment before using that command again."
    else:
        message = "Something went wrong. The bot logged the error for troubleshooting."
    with contextlib.suppress(discord.HTTPException, RuntimeError):
        await respond_error(interaction, message)


async def health_check() -> dict[str, Any]:
    """Return a compact diagnostic record useful for future admin commands."""
    normal = await bot.database.get_snapshot("normal")
    mirage = await bot.database.get_snapshot("mirage")
    return {
        "bot_ready": bot.is_ready(),
        "guild_count": len(bot.guilds),
        "normal_fruits": len(normal.fruits) if normal else 0,
        "mirage_fruits": len(mirage.fruits) if mirage else 0,
        "source_last_success": bot.source.last_success_at,
        "source_last_error": bot.source.last_error,
        "database_path": str(DATABASE_PATH.resolve()),
    }


def read_token() -> Optional[str]:
    """Read token from cloud environment first, then bot_token.txt."""
    token = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN")
    if token and token.strip():
        return token.strip()
    if TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
            return token or None
        except OSError:
            log.exception("Could not read bot_token.txt")
    return None


async def main() -> None:
    """Start the Discord client and release HTTP resources on exit."""
    token = read_token()
    if not token:
        raise RuntimeError(
            "Discord token missing. Set DISCORD_TOKEN in the host environment "
            "or create bot_token.txt next to main.py."
        )
    try:
        await bot.start(token)
    finally:
        await bot.source.close()


def run() -> None:
    """Synchronous entry point used by cloud hosts."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    except Exception:
        log.exception("Bot stopped unexpectedly")
        raise


if __name__ == "__main__":
    run()


# ---------------------------------------------------------------------------
# Operator notes: parsing, deployment, and troubleshooting
# ---------------------------------------------------------------------------
#
# Why direct HTML parsing?
# ------------------------
# This bot requests the same public stock page a browser can visit and parses
# the returned HTML. It does not require an API key. The stock page is the
# source of truth for the current stock snapshot. The item pages are consulted
# only when /stats needs an image that was not included in the stock HTML.
#
# Polling behavior
# ----------------
# A successful read creates a normalized fingerprint for each dealer. A
# fingerprint is based on fruit names, Beli prices, Robux prices, and rarity.
# Cosmetic HTML changes do not trigger alerts. If the stock fingerprint is the
# same as the previous reading, no alert is sent. The initial read seeds the
# JSON database without sending a notification, preventing a surprise alert
# when the bot is first deployed.
#
# Poll interval detail
# --------------------
# POLL_INTERVAL_SECONDS is set to 100, as requested. That is 1 minute and 40
# seconds. A 90-second interval would be 1 minute and 30 seconds. If the host
# has strict request limits, increase the interval rather than reducing it.
#
# Why no empty-stock alert?
# -------------------------
# A temporary website failure or HTML layout change could make a parser return
# an empty list. The bot refuses to overwrite a good saved snapshot with an
# empty snapshot. This avoids sending a false alert or losing the last known
# stock history. Look for "Parser did not find" in logs if the site changes.
#
# Role ping behavior
# ------------------
# The role configured with /stockalert is only mentioned if the newly detected
# stock includes a fruit whose rarity is Legendary or Mythical. Other stock
# changes still create an embed but do not ping the role. The bot disables
# @everyone and user mentions on the alert message. Discord's role hierarchy
# and role mention permissions still apply.
#
# W and L reactions
# -----------------
# The bot uses the standard Unicode 🆆 and 🅻 button-style reactions for W and L. If the server has custom W and L
# emoji, replace the two add_reaction strings in announce_stock_change with
# the server's exact custom emoji markup. The bot needs Use External Emojis
# only when reacting with custom emoji from another server.
#
# Image handling
# --------------
# When an item card exposes an image URL, the parser uses it directly. If not,
# /stats requests the public item page and checks its Open Graph image metadata.
# The bot does not need local JPG filenames. If the website provides no public
# image metadata for a fruit, the embed will still work without a thumbnail.
#
# JSON persistence
# ----------------
# stock_data.json stores current snapshots, a bounded history, last-seen
# records, alert destinations, auto-last-seen destinations, and poll errors.
# The file is written atomically. In cloud environments, attach a persistent
# volume to the directory containing main.py and stock_data.json if you want
# data to survive redeploys. Environment variables and code are not changed by
# the bot. The token is never written into the JSON database.
#
# Global command registration
# ---------------------------
# Slash commands registered globally can take some time to appear after the
# first startup. For testing, define TEST_GUILD_ID in the host environment.
# The bot then copies commands to that guild for faster command refresh. Once
# commands are verified, remove TEST_GUILD_ID to use global commands.
#
# Common deployment checks
# ------------------------
# - Confirm the process start command is `python main.py`.
# - Confirm requirements.txt is installed at build time.
# - Set DISCORD_TOKEN as a secret environment variable.
# - Enable Server Members Intent in the Developer Portal for member totals.
# - Ensure the bot role is above the role that /stockalert should ping.
# - Grant View Channel, Send Messages, Embed Links, Add Reactions, and Read
#   Message History to the configured stock channels.
# - Confirm the cloud host keeps the process running continuously.
# - Confirm outbound HTTPS access to fruityblox.com is allowed.
#
# Troubleshooting HTTP failures
# -----------------------------
# HTTP 403 can indicate that the site is rejecting automated traffic. HTTP 429
# indicates rate limiting. Do not repeatedly restart the bot to bypass a site
# restriction; wait and check the site's usage terms. HTTP 5xx errors are often
# temporary. The client retries a few times, logs the error, and waits until
# the next scheduled polling pass.
#
# Troubleshooting no alerts
# -------------------------
# - Use /stock type:Normal to check the cached current snapshot.
# - Check that /stockalert was configured in the correct server.
# - Confirm the selected dealer matches the configured alert dealer.
# - Check logs for "stock changed" or "stock unchanged".
# - Verify that the source page has not changed its HTML structure.
# - Remember that the first reading after a fresh database is saved silently.
#
# Troubleshooting wrong fruit image
# ---------------------------------
# Images are taken from item-card HTML or Open Graph metadata. A site's image
# can be a general icon rather than a game-specific render. If the site changes
# its markup, the image parser can be adjusted to match the new data attributes.
#
# Data safety
# -----------
# Do not upload bot_token.txt or paste the token into public source code.
# If a token is exposed, reset it in the Discord Developer Portal. Use your
# hosting provider's secret/environment variable support when available.
#
# Extending this project
# ----------------------
# Keep network requests asynchronous. Avoid blocking calls in event handlers.
# Use the StockDatabase methods for writes so concurrent polling and commands
# do not corrupt JSON. Keep snapshot comparison deterministic so a minor
# formatting difference does not create duplicate alerts. Add a schema
# migration before changing the shape of persisted records in a future version.
#
# The parser intentionally uses standard HTML tags and links rather than
# undocumented API endpoints. This is more maintainable when a public page is
# available, though it is still sensitive to changes in page markup.
#
# Extra deployment reminders
# --------------------------
# A bot that uses a public source website should make requests politely and
# respect the website's rules. The 100-second poll interval is the requested
# setting. Avoid running several copies of the same bot with the same database,
# because duplicate processes can create duplicate alerts and race on writes.
#
# If you run multiple replicas, use one replica only unless you replace this
# JSON persistence layer with a shared transactional database and a distributed
# leader lock. A local JSON file is designed for one running bot process.
#
# The stock image in the Discord embed is a thumbnail, so Discord positions it
# on the right side on desktop layouts. On mobile it may appear above or below
# text depending on the client. The blank lines in descriptions intentionally
# create the extra spacing requested in the reference screenshot.
#
# Commands are slash commands rather than prefix commands. Users do not need
# to type a prefix, and Discord will display each command's parameter choices.
#
# /stockalert is restricted to Administrators. /autolast-seen is also
# restricted to Administrators. Read-only commands can be used by members.
#
# End of operator notes.
