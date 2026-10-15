"""
Voltix Stock Alerts - Blox Fruits stock bot
===========================================

A Discord bot (discord.py 2.5+) that watches https://fruityblox.com/stock,
posts the Normal / Mirage dealer stock whenever it changes, pings a role when a
Legendary or Mythical fruit shows up, and reacts with W / L on every post.

Slash commands
--------------
/stockalert        channel role type     Set up automatic stock posts (Normal or Mirage)
/stockalert-remove type                  Remove an automatic stock post setup
/stock                                   Show the current stock (never auto-posts)
/last-seen         [fruit]               When a fruit was last in stock
/autolast-seen     channel               (Admins) post the "last seen" list on every stock change
/autolast-seen-off                       (Admins) stop the auto last-seen posts
/stats             fruit                 Price / rarity / type of a fruit + its picture
/alerts                                  Show this server's configuration
/timepassed                              How long the bot has been online
/ping                                    Gateway latency
/help                                    Command overview

Environment variables
---------------------
DISCORD_TOKEN   (required) bot token
DATA_DIR        (optional) folder for the SQLite file, default ./data
                Point this at a persistent volume on your host, otherwise the
                "last seen" history is lost whenever the container is rebuilt.
SYNC_COMMANDS   (optional) "0" to skip slash command syncing on start
LOG_LEVEL       (optional) DEBUG / INFO / WARNING (default INFO)

Emojis
------
The bot uses *application emojis* (the ones you uploaded in the Developer
Portal). They are matched by name (Rocket, Spin, Dark, Dollar ...), so nothing
needs to be hard-coded. If an emoji is missing the bot falls back to a default
unicode emoji.

Requires Python 3.10+
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import aiohttp
import discord
from bs4 import BeautifulSoup, Tag
from discord import app_commands
from discord.ext import commands, tasks

try:  # optional: load a local .env file when python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# ============================================================================
# 1. CONFIGURATION
# ============================================================================

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DB_PATH = DATA_DIR / "voltix.sqlite3"
SYNC_COMMANDS = os.getenv("SYNC_COMMANDS", "1") != "0"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

SITE_BASE = "https://fruityblox.com"
STOCK_URL = f"{SITE_BASE}/stock"

CHECK_INTERVAL = 100          # seconds between stock checks (1m 40s)
PRESENCE_INTERVAL = 300       # seconds between presence refreshes
HTTP_TIMEOUT = 20             # seconds per HTTP request
FETCH_RETRIES = 3             # attempts per download
SNAPSHOT_TTL = 30             # seconds a scraped snapshot is reused by commands
SEND_CONCURRENCY = 8          # simultaneous channel sends during a broadcast
LAST_SEEN_PAGE_SIZE = 8       # fruits per page in /last-seen
AUTO_LAST_SEEN_LIMIT = 20     # fruits listed in each auto last-seen post

EMBED_COLOR = 0x8E44FF        # purple, like the reference screenshot
SUCCESS_COLOR = 0x2ECC71
ERROR_COLOR = 0xE74C3C

WIN_EMOJI = "\U0001F1FC"      # regional indicator W
LOSE_EMOJI = "\U0001F1F1"     # regional indicator L
DEFAULT_FRUIT_EMOJI = "\U0001F348"   # melon
DEFAULT_DOLLAR_EMOJI = "\U0001F4B2"  # heavy dollar sign

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HTTP_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# ============================================================================
# 2. LOGGING
# ============================================================================

def setup_logging() -> None:
    """Log to stdout (what most cloud hosts capture)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(LOG_LEVEL)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


log = logging.getLogger("voltix")


# ============================================================================
# 3. CONSTANTS: STOCK TYPES AND RARITIES
# ============================================================================

@dataclass(frozen=True)
class StockTypeInfo:
    """Static description of a dealer."""

    key: str
    label: str
    period: int  # seconds between rotations


STOCK_TYPES: dict[str, StockTypeInfo] = {
    "normal": StockTypeInfo("normal", "Normal", 4 * 3600),
    "mirage": StockTypeInfo("mirage", "Mirage", 2 * 3600),
}

# Rarity table. The site itself lists the fruit *type* (Natural, Elemental,
# Beast ...) rather than the rarity, so the rarity is kept here. If the page
# ever shows a rarity word the scraper will prefer that over this table.
# Add new fruits here when the game adds them.
RARITY_BY_FRUIT: dict[str, str] = {}
for _rarity, _names in {
    "Common": ["rocket", "spin", "blade", "spring", "bomb", "smoke", "spike"],
    "Uncommon": ["flame", "falcon", "ice", "sand", "dark", "eagle", "diamond"],
    "Rare": ["light", "rubber", "ghost", "magma"],
    "Legendary": [
        "quake", "buddha", "love", "creation", "spider", "sound", "phoenix",
        "pheonix", "portal", "lightning", "rumble", "pain", "blizzard",
        "magnet",
    ],
    "Mythical": [
        "gravity", "mammoth", "trex", "dough", "shadow", "venom", "gas",
        "spirit", "tiger", "yeti", "kitsune", "control", "dragon", "leopard",
    ],
}.items():
    for _name in _names:
        RARITY_BY_FRUIT[_name] = _rarity

RARITY_ORDER = ["Common", "Uncommon", "Rare", "Legendary", "Mythical"]
PING_RARITIES = {"Legendary", "Mythical"}
RARITY_COLORS = {
    "Common": 0xB0B0B0,
    "Uncommon": 0x4FC3F7,
    "Rare": 0x5C6BC0,
    "Legendary": 0xAB47BC,
    "Mythical": 0xE53935,
    "Unknown": EMBED_COLOR,
}

# Emoji aliases: website name -> other names your emoji might use.
EMOJI_ALIASES: dict[str, list[str]] = {
    "phoenix": ["pheonix"],      # the emoji in the portal is spelled "Pheonix"
    "pheonix": ["phoenix"],
    "lightning": ["rumble"],
    "rumble": ["lightning"],
    "trex": ["trex", "t_rex"],
}


# ============================================================================
# 4. SMALL HELPERS
# ============================================================================

def normalize_key(text: str) -> str:
    """'T-Rex' -> 'trex', 'Dragon (East)' -> 'dragon'. Used as the fruit id."""
    text = re.sub(r"\(.*?\)", "", text or "")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def rarity_of(name: str) -> str:
    return RARITY_BY_FRUIT.get(normalize_key(name), "Unknown")


def format_duration(seconds: float) -> str:
    """3725 -> '1h 2m 5s'."""
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def next_reset_ts(stock_type: str, now: Optional[float] = None) -> int:
    """Next rotation as a unix timestamp.

    Normal rotates every 4h and Mirage every 2h, both aligned to UTC midnight
    (this matches the times in the reference screenshots).
    """
    period = STOCK_TYPES[stock_type].period
    now = int(now if now is not None else time.time())
    return (now // period + 1) * period


def ts_full(ts: int) -> str:
    """Discord timestamp: full date + relative."""
    return f"<t:{int(ts)}:F> (<t:{int(ts)}:R>)"


def chunked(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def stock_signature(fruits: Iterable["Fruit"]) -> str:
    """Stable fingerprint of a stock list (names + prices)."""
    return "|".join(sorted(f"{f.key}:{f.price}" for f in fruits))


# ============================================================================
# 5. DATA MODELS
# ============================================================================

@dataclass(slots=True)
class Fruit:
    """One fruit as listed by a dealer."""

    slug: str
    name: str
    ftype: str = "Unknown"
    price: int = 0
    robux: Optional[int] = None
    rarity: str = "Unknown"
    image_url: Optional[str] = None

    @property
    def key(self) -> str:
        return normalize_key(self.name) or normalize_key(self.slug)


@dataclass(slots=True)
class StockSnapshot:
    """Result of one scrape. A section is None if it could not be read."""

    normal: Optional[list[Fruit]] = None
    mirage: Optional[list[Fruit]] = None
    fetched_at: float = field(default_factory=time.time)

    def get(self, stock_type: str) -> Optional[list[Fruit]]:
        return getattr(self, stock_type, None)

    def all_fruits(self) -> list[Fruit]:
        out: list[Fruit] = []
        for st in STOCK_TYPES:
            out.extend(self.get(st) or [])
        return out

    def current_keys(self) -> set[str]:
        return {f.key for f in self.all_fruits()}


@dataclass(slots=True)
class LastSeen:
    """When a fruit was last observed in any dealer's stock."""

    key: str
    name: str
    stock_type: str
    price: int
    robux: Optional[int]
    seen_at: int
    image_url: Optional[str]
    rarity: str


# ============================================================================
# 6. DATABASE (SQLite, accessed through a worker thread)
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    guild_id   INTEGER NOT NULL,
    stock_type TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    role_id    INTEGER,
    PRIMARY KEY (guild_id, stock_type)
);

CREATE TABLE IF NOT EXISTS autolast (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS state (
    stock_type TEXT PRIMARY KEY,
    signature  TEXT    NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fruits (
    key        TEXT PRIMARY KEY,
    slug       TEXT NOT NULL,
    name       TEXT NOT NULL,
    ftype      TEXT,
    price      INTEGER,
    robux      INTEGER,
    rarity     TEXT,
    image_url  TEXT,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS last_seen (
    key        TEXT    NOT NULL,
    stock_type TEXT    NOT NULL,
    price      INTEGER,
    robux      INTEGER,
    seen_at    INTEGER NOT NULL,
    PRIMARY KEY (key, stock_type)
);
"""


class Database:
    """Thin async wrapper around one sqlite3 connection."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    # -- lifecycle ----------------------------------------------------------
    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)
        log.info("Database ready at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    # -- primitives ---------------------------------------------------------
    async def _run(self, fn):
        async with self._lock:
            return await asyncio.to_thread(fn)

    async def execute(self, sql: str, params: tuple = ()) -> int:
        def _do() -> int:
            assert self._conn is not None
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

        return await self._run(_do)

    async def executemany(self, sql: str, seq: list[tuple]) -> None:
        if not seq:
            return

        def _do() -> None:
            assert self._conn is not None
            self._conn.executemany(sql, seq)
            self._conn.commit()

        await self._run(_do)

    async def fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        def _do() -> list[sqlite3.Row]:
            assert self._conn is not None
            return self._conn.execute(sql, params).fetchall()

        return await self._run(_do)

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        def _do() -> Optional[sqlite3.Row]:
            assert self._conn is not None
            return self._conn.execute(sql, params).fetchone()

        return await self._run(_do)

    # -- stock alert subscriptions -----------------------------------------
    async def set_alert(
        self, guild_id: int, stock_type: str, channel_id: int, role_id: Optional[int]
    ) -> None:
        await self.execute(
            "INSERT INTO alerts (guild_id, stock_type, channel_id, role_id) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, stock_type) DO UPDATE SET "
            "channel_id=excluded.channel_id, role_id=excluded.role_id",
            (guild_id, stock_type, channel_id, role_id),
        )

    async def remove_alert(self, guild_id: int, stock_type: str) -> bool:
        removed = await self.execute(
            "DELETE FROM alerts WHERE guild_id=? AND stock_type=?",
            (guild_id, stock_type),
        )
        return removed > 0

    async def get_alerts(self, stock_type: str) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM alerts WHERE stock_type=?", (stock_type,)
        )

    async def get_guild_alerts(self, guild_id: int) -> list[sqlite3.Row]:
        return await self.fetchall(
            "SELECT * FROM alerts WHERE guild_id=?", (guild_id,)
        )

    # -- auto last-seen subscriptions --------------------------------------
    async def set_autolast(self, guild_id: int, channel_id: int) -> None:
        await self.execute(
            "INSERT INTO autolast (guild_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, channel_id),
        )

    async def remove_autolast(self, guild_id: int) -> bool:
        removed = await self.execute(
            "DELETE FROM autolast WHERE guild_id=?", (guild_id,)
        )
        return removed > 0

    async def get_autolasts(self) -> list[sqlite3.Row]:
        return await self.fetchall("SELECT * FROM autolast")

    async def get_guild_autolast(self, guild_id: int) -> Optional[sqlite3.Row]:
        return await self.fetchone(
            "SELECT * FROM autolast WHERE guild_id=?", (guild_id,)
        )

    async def drop_guild(self, guild_id: int) -> None:
        await self.execute("DELETE FROM alerts WHERE guild_id=?", (guild_id,))
        await self.execute("DELETE FROM autolast WHERE guild_id=?", (guild_id,))

    # -- last known stock (for change detection) ---------------------------
    async def get_state(self, stock_type: str) -> Optional[str]:
        row = await self.fetchone(
            "SELECT signature FROM state WHERE stock_type=?", (stock_type,)
        )
        return None if row is None else str(row["signature"])

    async def set_state(self, stock_type: str, signature: str) -> None:
        await self.execute(
            "INSERT INTO state (stock_type, signature, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(stock_type) DO UPDATE SET "
            "signature=excluded.signature, updated_at=excluded.updated_at",
            (stock_type, signature, int(time.time())),
        )

    # -- fruit catalogue and last-seen history -----------------------------
    async def record_stock(self, stock_type: str, fruits: list[Fruit], now: int) -> None:
        """Remember every fruit currently in stock (details + last-seen time)."""
        fruit_rows = [
            (
                f.key, f.slug, f.name, f.ftype, f.price, f.robux,
                f.rarity, f.image_url, now,
            )
            for f in fruits
        ]
        seen_rows = [(f.key, stock_type, f.price, f.robux, now) for f in fruits]
        await self.executemany(
            "INSERT INTO fruits (key, slug, name, ftype, price, robux, rarity, image_url, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "slug=excluded.slug, name=excluded.name, ftype=excluded.ftype, "
            "price=excluded.price, robux=excluded.robux, rarity=excluded.rarity, "
            "image_url=COALESCE(excluded.image_url, fruits.image_url), "
            "updated_at=excluded.updated_at",
            fruit_rows,
        )
        await self.executemany(
            "INSERT INTO last_seen (key, stock_type, price, robux, seen_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key, stock_type) DO UPDATE SET "
            "price=excluded.price, robux=excluded.robux, seen_at=excluded.seen_at",
            seen_rows,
        )

    async def set_fruit_image(self, key: str, image_url: str) -> None:
        await self.execute(
            "UPDATE fruits SET image_url=? WHERE key=?", (image_url, key)
        )

    async def get_fruit(self, key: str) -> Optional[Fruit]:
        row = await self.fetchone("SELECT * FROM fruits WHERE key=?", (key,))
        if row is None:
            return None
        return Fruit(
            slug=row["slug"],
            name=row["name"],
            ftype=row["ftype"] or "Unknown",
            price=int(row["price"] or 0),
            robux=row["robux"],
            rarity=row["rarity"] or rarity_of(row["name"]),
            image_url=row["image_url"],
        )

    async def list_fruit_names(self) -> list[str]:
        rows = await self.fetchall("SELECT name FROM fruits ORDER BY name")
        return [str(r["name"]) for r in rows]

    async def get_last_seen_all(self) -> list[LastSeen]:
        """Latest sighting of every known fruit (across both dealers)."""
        rows = await self.fetchall(
            "SELECT ls.key, ls.stock_type, ls.price, ls.robux, ls.seen_at, "
            "f.name, f.image_url, f.rarity "
            "FROM last_seen ls JOIN fruits f ON f.key = ls.key"
        )
        best: dict[str, LastSeen] = {}
        for r in rows:
            entry = LastSeen(
                key=r["key"],
                name=r["name"],
                stock_type=r["stock_type"],
                price=int(r["price"] or 0),
                robux=r["robux"],
                seen_at=int(r["seen_at"]),
                image_url=r["image_url"],
                rarity=r["rarity"] or rarity_of(r["name"]),
            )
            current = best.get(entry.key)
            if current is None or entry.seen_at > current.seen_at:
                best[entry.key] = entry
        return list(best.values())

    async def get_last_seen_for(self, key: str) -> list[LastSeen]:
        """Sightings of one fruit, one entry per dealer."""
        rows = await self.fetchall(
            "SELECT ls.key, ls.stock_type, ls.price, ls.robux, ls.seen_at, "
            "f.name, f.image_url, f.rarity "
            "FROM last_seen ls JOIN fruits f ON f.key = ls.key WHERE ls.key=?",
            (key,),
        )
        return [
            LastSeen(
                key=r["key"],
                name=r["name"],
                stock_type=r["stock_type"],
                price=int(r["price"] or 0),
                robux=r["robux"],
                seen_at=int(r["seen_at"]),
                image_url=r["image_url"],
                rarity=r["rarity"] or rarity_of(r["name"]),
            )
            for r in rows
        ]


# ============================================================================
# 7. EMOJI MANAGER (application emojis uploaded in the Developer Portal)
# ============================================================================

class EmojiManager:
    """Looks up the bot's application emojis by fruit name."""

    def __init__(self) -> None:
        self._by_key: dict[str, discord.Emoji] = {}
        self.refreshed_at: float = 0.0

    async def refresh(self, bot: commands.Bot) -> None:
        try:
            emojis = await bot.fetch_application_emojis()
        except AttributeError:
            log.error(
                "This discord.py version has no application emoji support. "
                "Upgrade with: pip install -U 'discord.py>=2.5'"
            )
            return
        except discord.HTTPException as exc:
            log.warning("Could not fetch application emojis: %s", exc)
            return
        self._by_key = {normalize_key(e.name): e for e in emojis}
        self.refreshed_at = time.time()
        log.info("Loaded %d application emojis", len(self._by_key))

    def _lookup(self, name: str) -> Optional[discord.Emoji]:
        key = normalize_key(name)
        for candidate in (key, *EMOJI_ALIASES.get(key, [])):
            emoji = self._by_key.get(normalize_key(candidate))
            if emoji is not None:
                return emoji
        return None

    def fruit(self, name: str) -> str:
        emoji = self._lookup(name)
        return str(emoji) if emoji else DEFAULT_FRUIT_EMOJI

    def dollar(self) -> str:
        emoji = self._by_key.get("dollar")
        return str(emoji) if emoji else DEFAULT_DOLLAR_EMOJI

    def __len__(self) -> int:
        return len(self._by_key)


# ============================================================================
# 8. SCRAPER (fruityblox.com/stock, plain HTML parsing, no API key)
# ============================================================================

ITEM_HREF_RE = re.compile(r"/items/([A-Za-z0-9_\-]+)")
NUMBER_RE = re.compile(r"\d[\d,]*")
RARITY_WORD_RE = re.compile(r"\b(Common|Uncommon|Rare|Legendary|Mythical)\b", re.I)
SECTION_TITLES = {
    "normal": "normal",
    "normal stock": "normal",
    "mirage": "mirage",
    "mirage stock": "mirage",
}


def extract_image_url(img: Tag) -> Optional[str]:
    """Return the real image URL of an <img>, unwrapping Next.js proxies."""
    src: Optional[str] = None
    for attr in ("src", "data-src", "data-lazy-src"):
        value = img.get(attr)
        if value and not str(value).startswith("data:"):
            src = str(value)
            break
    if src is None:
        srcset = img.get("srcset")
        if srcset:
            src = str(srcset).split(",")[0].strip().split(" ")[0]
    if not src:
        return None
    if "/_next/image" in src:
        qs = parse_qs(urlparse(src).query)
        if qs.get("url"):
            src = unquote(qs["url"][0])
    return urljoin(SITE_BASE + "/", src)


def parse_fruit_anchor(anchor: Tag, slug: str) -> Optional[Fruit]:
    """Turn one <a href="/items/rocket">...</a> card into a Fruit."""
    strings = [s for s in anchor.stripped_strings]
    text = " ".join(strings)
    img = anchor.find("img")
    image_url = extract_image_url(img) if isinstance(img, Tag) else None

    # Name: first text chunk that looks like a word (not a number / currency)
    name: Optional[str] = None
    for chunk in strings:
        if re.search(r"[A-Za-z]", chunk) and not re.search(r"\d", chunk):
            if chunk.strip().lower() in {"r", "r$", "beli", "robux"}:
                continue
            name = chunk.strip()
            break
    if name is None and isinstance(img, Tag) and img.get("alt"):
        name = str(img["alt"]).strip()
    if not name:
        name = slug.replace("-", " ").title()

    # Type (Natural / Elemental / Beast / ...): next word-like chunk
    ftype = "Unknown"
    rarity_from_page: Optional[str] = None
    for chunk in strings:
        low = chunk.strip().lower()
        if low == name.lower() or low in {"r", "r$", "beli", "robux"}:
            continue
        if re.search(r"\d", chunk):
            continue
        match = RARITY_WORD_RE.fullmatch(chunk.strip())
        if match:
            rarity_from_page = match.group(1).title()
            continue
        if re.search(r"[A-Za-z]", chunk):
            ftype = chunk.strip()
            break

    numbers = NUMBER_RE.findall(text)
    if not numbers:
        log.debug("No price found for %s (%r)", slug, text)
        return None
    price = int(numbers[0].replace(",", ""))
    robux = int(numbers[1].replace(",", "")) if len(numbers) > 1 else None

    if rarity_from_page is None:
        found = RARITY_WORD_RE.search(text)
        if found:
            rarity_from_page = found.group(1).title()

    return Fruit(
        slug=slug.lower(),
        name=name,
        ftype=ftype,
        price=price,
        robux=robux,
        rarity=rarity_from_page or rarity_of(name),
        image_url=image_url,
    )


def parse_stock_html(html: str) -> Optional[StockSnapshot]:
    """Parse the stock page.

    The page lists two sections, headed "Normal" and "Mirage"; every fruit is
    a link to /items/<slug>. We walk headings and links in document order and
    assign each link to the section heading above it.
    """
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, list[Fruit]] = {"normal": [], "mirage": []}
    headers_found: set[str] = set()
    current: Optional[str] = None

    for el in soup.find_all(["h1", "h2", "h3", "h4", "a"]):
        if el.name != "a":
            if el.find_parent("a") is not None:
                continue  # a heading used as the fruit's name inside a card
            title = el.get_text(" ", strip=True).lower()
            if title in SECTION_TITLES:
                current = SECTION_TITLES[title]
                headers_found.add(current)
            elif el.name in ("h1", "h2") and current is not None:
                current = None  # another major heading closes the section
            continue

        if current is None:
            continue
        match = ITEM_HREF_RE.search(str(el.get("href", "")))
        if not match:
            continue
        fruit = parse_fruit_anchor(el, match.group(1))
        if fruit is None:
            continue
        if all(existing.key != fruit.key for existing in sections[current]):
            sections[current].append(fruit)

    if not headers_found:
        return None

    snapshot = StockSnapshot()
    for stock_type, fruits in sections.items():
        # An empty list means "could not read it", never "empty dealer".
        setattr(snapshot, stock_type, fruits or None)
    if snapshot.normal is None and snapshot.mirage is None:
        return None
    return snapshot


def parse_item_image(html: str) -> Optional[str]:
    """Image URL from an individual /items/<slug> page."""
    soup = BeautifulSoup(html, "html.parser")
    meta = soup.find("meta", attrs={"property": "og:image"})
    if isinstance(meta, Tag) and meta.get("content"):
        return urljoin(SITE_BASE + "/", str(meta["content"]))
    for img in soup.find_all("img"):
        if isinstance(img, Tag):
            url = extract_image_url(img)
            if url and "logo" not in url.lower() and "favicon" not in url.lower():
                return url
    return None


class StockScraper:
    """Downloads and parses the stock page, with a short-lived cache."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self._snapshot: Optional[StockSnapshot] = None
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self.last_success_at: float = 0.0

    async def _download(self, url: str) -> Optional[str]:
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        for attempt in range(1, FETCH_RETRIES + 1):
            try:
                async with self.session.get(
                    url, headers=HTTP_HEADERS, timeout=timeout
                ) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    self.last_error = f"HTTP {resp.status} from {url}"
                    log.warning("%s (attempt %d/%d)", self.last_error, attempt, FETCH_RETRIES)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("Fetch failed (attempt %d/%d): %s", attempt, FETCH_RETRIES, exc)
            if attempt < FETCH_RETRIES:
                await asyncio.sleep(2 * attempt)
        return None

    async def get_snapshot(self, force: bool = False) -> Optional[StockSnapshot]:
        """Current stock. Reuses a snapshot younger than SNAPSHOT_TTL seconds."""
        async with self._lock:
            cached = self._snapshot
            if (
                not force
                and cached is not None
                and time.time() - cached.fetched_at < SNAPSHOT_TTL
            ):
                return cached

            html = await self._download(STOCK_URL)
            if html is None:
                return cached  # stale data beats no data for /stock
            snapshot = await asyncio.to_thread(parse_stock_html, html)
            if snapshot is None:
                self.last_error = "Could not find the Normal/Mirage sections on the page"
                log.error(self.last_error)
                return cached
            self._snapshot = snapshot
            self.last_error = None
            self.last_success_at = time.time()
            return snapshot

    async def fetch_item_image(self, slug: str) -> Optional[str]:
        """Look up a fruit picture on its own page (for fruits not in stock)."""
        html = await self._download(f"{SITE_BASE}/items/{slug}")
        if html is None:
            return None
        return await asyncio.to_thread(parse_item_image, html)


# ============================================================================
# 9. EMBED BUILDERS
# ============================================================================

def price_line(fruit: Fruit, emojis: EmojiManager) -> str:
    """'<emoji> **Blade** — $ `30,000`' (same layout as the reference)."""
    return f"{emojis.fruit(fruit.name)} **{fruit.name}** — {emojis.dollar()} `{fruit.price:,}`"


def build_stock_embed(
    stock_type: str, fruits: list[Fruit], emojis: EmojiManager
) -> discord.Embed:
    info = STOCK_TYPES[stock_type]
    nxt = next_reset_ts(stock_type)
    lines = [price_line(f, emojis) for f in sorted(fruits, key=lambda f: (f.price, f.name))]
    description = f"**NEXT** update <t:{nxt}:R> · at <t:{nxt}:t>\n\n" + "\n".join(lines)
    return discord.Embed(
        title=f"Current Stock — {info.label}",
        description=description,
        color=EMBED_COLOR,
    )


def last_seen_line(entry: LastSeen, emojis: EmojiManager) -> str:
    """'<emoji> **Name** - $ `price`' then the date/time on the next line."""
    return (
        f"{emojis.fruit(entry.name)} **{entry.name}** - {emojis.dollar()} `{entry.price:,}`\n"
        f"{ts_full(entry.seen_at)}"
    )


def build_last_seen_embeds(
    entries: list[LastSeen],
    emojis: EmojiManager,
    current_keys: set[str],
    per_page: int = LAST_SEEN_PAGE_SIZE,
) -> list[discord.Embed]:
    """Paginated 'last seen' list, newest sighting first."""
    ordered = sorted(entries, key=lambda e: e.seen_at, reverse=True)
    pages = chunked(ordered, per_page) or [[]]
    embeds: list[discord.Embed] = []
    for index, page in enumerate(pages, start=1):
        lines: list[str] = []
        for entry in page:
            line = last_seen_line(entry, emojis)
            if entry.key in current_keys:
                line = line.split("\n")[0] + "\n🟢 **In stock right now**"
            lines.append(line)
        embed = discord.Embed(
            title="Last Seen",
            description="\n\n".join(lines) if lines else "No stock history recorded yet.",
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Page {index}/{len(pages)} • {len(ordered)} fruits tracked")
        embeds.append(embed)
    return embeds


def build_fruit_last_seen_embed(
    key_entries: list[LastSeen],
    fruit: Fruit,
    emojis: EmojiManager,
    current_keys: set[str],
) -> discord.Embed:
    """Last-seen details for a single fruit."""
    lines: list[str] = []
    for entry in sorted(key_entries, key=lambda e: e.seen_at, reverse=True):
        label = STOCK_TYPES[entry.stock_type].label
        if entry.key in current_keys and _in_current(entry, current_keys):
            when = "🟢 **In stock right now**"
        else:
            when = ts_full(entry.seen_at)
        lines.append(
            f"{emojis.fruit(entry.name)} **{entry.name}** - {emojis.dollar()} `{entry.price:,}`\n"
            f"{label} • {when}"
        )
    embed = discord.Embed(
        title=f"Last Seen — {fruit.name}",
        description="\n\n".join(lines),
        color=RARITY_COLORS.get(fruit.rarity, EMBED_COLOR),
    )
    if fruit.image_url:
        embed.set_thumbnail(url=fruit.image_url)
    return embed


def _in_current(entry: LastSeen, current_keys: set[str]) -> bool:
    """Helper kept separate so the intent is explicit: key is in stock now."""
    return entry.key in current_keys


def build_auto_last_seen_embed(
    entries: list[LastSeen], emojis: EmojiManager
) -> discord.Embed:
    """Auto post: fruits NOT in stock right now, longest absent first."""
    ordered = sorted(entries, key=lambda e: e.seen_at)[:AUTO_LAST_SEEN_LIMIT]
    lines = [last_seen_line(e, emojis) for e in ordered]
    description = "\n\n".join(lines) if lines else "Every tracked fruit is in stock right now."
    # Discord caps embed descriptions at 4096 characters.
    while len(description) > 4000 and lines:
        lines.pop()
        description = "\n\n".join(lines)
    embed = discord.Embed(
        title="Last Seen Stock",
        description=description,
        color=EMBED_COLOR,
    )
    embed.set_footer(text="Fruits not in stock right now • longest absent first")
    return embed


def build_stats_embed(
    fruit: Fruit,
    last_seen: list[LastSeen],
    in_stock_in: list[str],
    emojis: EmojiManager,
) -> discord.Embed:
    """Price card. The fruit picture sits on the right as the thumbnail."""
    rarity = fruit.rarity if fruit.rarity != "Unknown" else rarity_of(fruit.name)
    embed = discord.Embed(
        title=f"{emojis.fruit(fruit.name)} {fruit.name}",
        color=RARITY_COLORS.get(rarity, EMBED_COLOR),
    )
    beli = f"{emojis.dollar()} `{fruit.price:,}`" if fruit.price else "Unknown"
    embed.add_field(name="Beli price", value=beli, inline=True)
    robux = f"`R$ {fruit.robux:,}`" if fruit.robux else "Unknown"
    embed.add_field(name="Robux price", value=robux, inline=True)
    embed.add_field(name="Rarity", value=rarity, inline=True)
    embed.add_field(name="Type", value=fruit.ftype or "Unknown", inline=True)

    if in_stock_in:
        embed.add_field(
            name="Stock",
            value="🟢 In stock now: " + ", ".join(STOCK_TYPES[s].label for s in in_stock_in),
            inline=True,
        )
    else:
        embed.add_field(name="Stock", value="🔴 Not in stock", inline=True)

    if last_seen:
        newest = max(last_seen, key=lambda e: e.seen_at)
        embed.add_field(
            name="Last seen",
            value=f"{STOCK_TYPES[newest.stock_type].label} • {ts_full(newest.seen_at)}",
            inline=False,
        )
    else:
        embed.add_field(name="Last seen", value="Not seen since the bot started tracking.", inline=False)

    if fruit.image_url:
        embed.set_thumbnail(url=fruit.image_url)
    return embed


def simple_embed(title: str, description: str, color: int = EMBED_COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


# ============================================================================
# 10. VIEWS
# ============================================================================

def make_invite_view(url: Optional[str]) -> Optional[discord.ui.View]:
    """The 'Add me to your server!' link button under every stock post."""
    if not url:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.link,
            label="Add me to your server!",
            url=url,
            emoji="\U0001F517",
        )
    )
    return view


class PagedEmbedView(discord.ui.View):
    """Previous / next buttons for a list of embeds. Only the invoker can use it."""

    def __init__(self, author_id: int, pages: list[discord.Embed], timeout: float = 180) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.pages = pages
        self.index = 0
        self.message: Optional[discord.Message] = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.prev_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran the command can flip pages.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary, emoji="▶️")
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ============================================================================
# 11. THE BOT
# ============================================================================

class VoltixBot(commands.Bot):
    """Bot subclass holding shared services (db, scraper, emojis)."""

    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=discord.Intents.default(),
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.started_at: float = time.time()
        self.db = Database(DB_PATH)
        self.emojis_cache = EmojiManager()
        self.session: Optional[aiohttp.ClientSession] = None
        self.scraper: Optional[StockScraper] = None
        self.invite_url: Optional[str] = None

    async def setup_hook(self) -> None:
        await self.db.open()
        self.session = aiohttp.ClientSession()
        self.scraper = StockScraper(self.session)
        await self.emojis_cache.refresh(self)

        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            embed_links=True,
            add_reactions=True,
            read_message_history=True,
            use_external_emojis=True,
            mention_everyone=True,  # needed to ping roles that are not mentionable
        )
        if self.application_id:
            self.invite_url = discord.utils.oauth_url(
                self.application_id,
                permissions=permissions,
                scopes=("bot", "applications.commands"),
            )

        await self.add_cog(StockCog(self))
        self.tree.on_error = self.on_tree_error

        if SYNC_COMMANDS:
            synced = await self.tree.sync()
            log.info("Synced %d slash commands", len(synced))

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
        await self.db.close()
        await super().close()

    # -- presence -----------------------------------------------------------
    def counts(self) -> tuple[int, int]:
        servers = len(self.guilds)
        users = sum(g.member_count or 0 for g in self.guilds)
        return servers, users

    async def update_presence(self) -> None:
        servers, users = self.counts()
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"Servers {servers:,} • Users {users:,}",
        )
        try:
            await self.change_presence(status=discord.Status.online, activity=activity)
        except Exception:  # noqa: BLE001 - never let presence kill the bot
            log.exception("Failed to update presence")

    # -- events -------------------------------------------------------------
    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s) in %d servers", self.user, getattr(self.user, "id", "?"), len(self.guilds))
        await self.update_presence()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        log.info("Joined guild %s (%s)", guild.name, guild.id)
        await self.update_presence()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        log.info("Removed from guild %s (%s)", guild.name, guild.id)
        await self.db.drop_guild(guild.id)
        await self.update_presence()

    # -- slash command errors ----------------------------------------------
    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down! Try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You don't have permission to use this command."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command only works inside a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use this command."
        else:
            log.exception("Unhandled command error", exc_info=error)
            message = "Something went wrong on my side. Please try again in a moment."

        embed = simple_embed("Error", message, ERROR_COLOR)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass


# ============================================================================
# 12. THE COG: BACKGROUND LOOPS AND SLASH COMMANDS
# ============================================================================

STOCK_CHOICES = [
    app_commands.Choice(name="Mirage", value="mirage"),
    app_commands.Choice(name="Normal", value="normal"),
]
STOCK_CHOICES_WITH_BOTH = [*STOCK_CHOICES, app_commands.Choice(name="Both", value="both")]

REQUIRED_CHANNEL_PERMS = {
    "view_channel": "View Channel",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "add_reactions": "Add Reactions",
}


def missing_channel_perms(channel: discord.abc.GuildChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    return [label for attr, label in REQUIRED_CHANNEL_PERMS.items() if not getattr(perms, attr)]


class StockCog(commands.Cog):
    """Stock checking loop + every slash command."""

    def __init__(self, bot: VoltixBot) -> None:
        self.bot = bot
        self._send_sem = asyncio.Semaphore(SEND_CONCURRENCY)
        self.last_check_at: float = 0.0
        self.checks_done: int = 0
        self.changes_seen: int = 0
        self.stock_loop.start()
        self.presence_loop.start()
        self.emoji_loop.start()

    async def cog_unload(self) -> None:
        self.stock_loop.cancel()
        self.presence_loop.cancel()
        self.emoji_loop.cancel()

    # ------------------------------------------------------------------ utils
    @property
    def db(self) -> Database:
        return self.bot.db

    @property
    def emojis(self) -> EmojiManager:
        return self.bot.emojis_cache

    @property
    def scraper(self) -> StockScraper:
        assert self.bot.scraper is not None
        return self.bot.scraper

    async def resolve_fruit(self, text: str) -> Optional[Fruit]:
        """Find a fruit by (partial) name in the DB, else the rarity table."""
        key = normalize_key(text)
        if not key:
            return None
        fruit = await self.db.get_fruit(key)
        if fruit is not None:
            return fruit
        # partial match against names we have seen
        names = await self.db.list_fruit_names()
        for name in names:
            if key in normalize_key(name):
                return await self.db.get_fruit(normalize_key(name))
        # known fruit that has never been in stock while the bot was watching
        for known in RARITY_BY_FRUIT:
            if known == key or key in known:
                title = "T-Rex" if known == "trex" else known.title()
                return Fruit(
                    slug=known,
                    name=title,
                    rarity=RARITY_BY_FRUIT[known],
                )
        return None

    async def ensure_image(self, fruit: Fruit) -> None:
        """Fill in fruit.image_url from the site if we do not have one yet."""
        if fruit.image_url:
            return
        url = await self.scraper.fetch_item_image(fruit.slug)
        if url:
            fruit.image_url = url
            await self.db.set_fruit_image(fruit.key, url)

    # ------------------------------------------------------------- background
    @tasks.loop(seconds=CHECK_INTERVAL)
    async def stock_loop(self) -> None:
        try:
            await self.check_stock()
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("Stock check failed")

    @stock_loop.before_loop
    async def before_stock_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=PRESENCE_INTERVAL)
    async def presence_loop(self) -> None:
        await self.bot.update_presence()

    @presence_loop.before_loop
    async def before_presence_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(hours=1)
    async def emoji_loop(self) -> None:
        await self.emojis.refresh(self.bot)

    @emoji_loop.before_loop
    async def before_emoji_loop(self) -> None:
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------- stock checking
    async def check_stock(self) -> None:
        """Scrape once, record sightings, and broadcast whatever changed."""
        snapshot = await self.scraper.get_snapshot(force=True)
        self.last_check_at = time.time()
        if snapshot is None:
            log.warning("No stock data available this round")
            return
        self.checks_done += 1

        now = int(time.time())
        changed: list[tuple[str, list[Fruit]]] = []
        for stock_type in STOCK_TYPES:
            fruits = snapshot.get(stock_type)
            if not fruits:
                continue
            await self.db.record_stock(stock_type, fruits, now)
            signature = stock_signature(fruits)
            previous = await self.db.get_state(stock_type)
            if previous != signature:
                await self.db.set_state(stock_type, signature)
                changed.append((stock_type, fruits))

        if not changed:
            log.debug("Stock unchanged")
            return

        self.changes_seen += 1
        log.info("Stock changed: %s", ", ".join(t for t, _ in changed))
        for stock_type, fruits in changed:
            await self.broadcast_stock(stock_type, fruits)
        await self.broadcast_last_seen(snapshot)

    async def broadcast_stock(self, stock_type: str, fruits: list[Fruit]) -> None:
        """Send the stock embed to every server that subscribed to this dealer."""
        alerts = await self.db.get_alerts(stock_type)
        if not alerts:
            return
        embed = build_stock_embed(stock_type, fruits, self.emojis)
        should_ping = any(f.rarity in PING_RARITIES for f in fruits)
        view_url = self.bot.invite_url
        await asyncio.gather(
            *(
                self._deliver_stock(row, stock_type, embed, should_ping, view_url)
                for row in alerts
            ),
            return_exceptions=True,
        )

    async def _deliver_stock(
        self,
        row,
        stock_type: str,
        embed: discord.Embed,
        should_ping: bool,
        invite_url: Optional[str],
    ) -> None:
        async with self._send_sem:
            guild_id = int(row["guild_id"])
            channel_id = int(row["channel_id"])
            role_id = row["role_id"]
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    log.info("Channel %s is gone, removing %s alert for guild %s", channel_id, stock_type, guild_id)
                    await self.db.remove_alert(guild_id, stock_type)
                    return
                except discord.HTTPException as exc:
                    log.warning("Could not fetch channel %s: %s", channel_id, exc)
                    return
            if not isinstance(channel, discord.abc.Messageable):
                return

            content: Optional[str] = None
            mentions = discord.AllowedMentions.none()
            if should_ping and role_id:
                content = f"<@&{int(role_id)}>"
                mentions = discord.AllowedMentions(roles=[discord.Object(id=int(role_id))])

            try:
                message = await channel.send(
                    content=content,
                    embed=embed,
                    view=make_invite_view(invite_url) or discord.utils.MISSING,
                    allowed_mentions=mentions,
                )
            except discord.Forbidden:
                log.warning("Missing permissions to post in channel %s (guild %s)", channel_id, guild_id)
                return
            except discord.NotFound:
                await self.db.remove_alert(guild_id, stock_type)
                return
            except discord.HTTPException as exc:
                log.warning("Failed to send stock to %s: %s", channel_id, exc)
                return

            for reaction in (WIN_EMOJI, LOSE_EMOJI):
                try:
                    await message.add_reaction(reaction)
                except discord.HTTPException as exc:
                    log.debug("Could not react in %s: %s", channel_id, exc)
                    break

    async def broadcast_last_seen(self, snapshot: StockSnapshot) -> None:
        """Send the 'last seen' list to servers that enabled /autolast-seen."""
        targets = await self.db.get_autolasts()
        if not targets:
            return
        current = snapshot.current_keys()
        entries = [e for e in await self.db.get_last_seen_all() if e.key not in current]
        embed = build_auto_last_seen_embed(entries, self.emojis)
        await asyncio.gather(
            *(self._deliver_last_seen(row, embed) for row in targets),
            return_exceptions=True,
        )

    async def _deliver_last_seen(self, row, embed: discord.Embed) -> None:
        async with self._send_sem:
            guild_id = int(row["guild_id"])
            channel_id = int(row["channel_id"])
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.NotFound:
                    await self.db.remove_autolast(guild_id)
                    return
                except discord.HTTPException:
                    return
            if not isinstance(channel, discord.abc.Messageable):
                return
            try:
                await channel.send(embed=embed)
            except discord.NotFound:
                await self.db.remove_autolast(guild_id)
            except discord.HTTPException as exc:
                log.warning("Failed to send last-seen to %s: %s", channel_id, exc)

    # ================================================================ COMMANDS
    # ---------------------------------------------------------- /stockalert
    @app_commands.command(
        name="stockalert",
        description="Post the stock in a channel every time it changes.",
    )
    @app_commands.describe(
        channel="Channel where the stock will be posted",
        role="Role to ping when a Legendary or Mythical fruit is in stock",
        type="Which dealer to follow",
    )
    @app_commands.choices(type=STOCK_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def stockalert(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        role: Optional[discord.Role],
        type: app_commands.Choice[str],
    ) -> None:
        assert interaction.guild is not None
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                embed=simple_embed("No permission", "You need the **Manage Server** permission.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        missing = missing_channel_perms(channel, interaction.guild.me)
        if missing:
            await interaction.response.send_message(
                embed=simple_embed(
                    "I can't post there",
                    f"I'm missing these permissions in {channel.mention}: **{', '.join(missing)}**.",
                    ERROR_COLOR,
                ),
                ephemeral=True,
            )
            return

        stock_type = type.value
        role_id = role.id if role else None
        await self.db.set_alert(interaction.guild.id, stock_type, channel.id, role_id)

        notes = [
            f"**Dealer:** {STOCK_TYPES[stock_type].label}",
            f"**Channel:** {channel.mention}",
            f"**Ping role:** {role.mention if role else 'none'}",
        ]
        if role and not role.mentionable and not interaction.guild.me.guild_permissions.mention_everyone:
            notes.append(
                "\n⚠️ That role isn't mentionable and I don't have the **Mention Everyone** "
                "permission, so the ping will not notify anyone. Either make the role "
                "mentionable or give me that permission."
            )
        await interaction.response.send_message(
            embed=simple_embed("Stock alert enabled", "\n".join(notes), SUCCESS_COLOR),
            ephemeral=True,
        )

        # Post the current stock right away so the setup can be checked.
        snapshot = await self.scraper.get_snapshot()
        fruits = snapshot.get(stock_type) if snapshot else None
        if fruits:
            embed = build_stock_embed(stock_type, fruits, self.emojis)
            try:
                msg = await channel.send(
                    embed=embed,
                    view=make_invite_view(self.bot.invite_url) or discord.utils.MISSING,
                )
                for reaction in (WIN_EMOJI, LOSE_EMOJI):
                    await msg.add_reaction(reaction)
            except discord.HTTPException as exc:
                log.warning("Initial stock post failed: %s", exc)

    @app_commands.command(
        name="stockalert-remove",
        description="Stop the automatic stock posts for a dealer.",
    )
    @app_commands.describe(type="Which dealer to stop following")
    @app_commands.choices(type=STOCK_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def stockalert_remove(
        self, interaction: discord.Interaction, type: app_commands.Choice[str]
    ) -> None:
        assert interaction.guild is not None
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                embed=simple_embed("No permission", "You need the **Manage Server** permission.", ERROR_COLOR),
                ephemeral=True,
            )
            return
        removed = await self.db.remove_alert(interaction.guild.id, type.value)
        text = (
            f"The **{STOCK_TYPES[type.value].label}** stock alert was removed."
            if removed
            else f"There was no **{STOCK_TYPES[type.value].label}** stock alert set up."
        )
        await interaction.response.send_message(
            embed=simple_embed("Stock alert", text, SUCCESS_COLOR if removed else EMBED_COLOR),
            ephemeral=True,
        )

    # --------------------------------------------------------------- /stock
    @app_commands.command(
        name="stock",
        description="Show the current Blox Fruits stock.",
    )
    @app_commands.describe(type="Which dealer to show (default: both)")
    @app_commands.choices(type=STOCK_CHOICES_WITH_BOTH)
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
    async def stock(
        self,
        interaction: discord.Interaction,
        type: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        snapshot = await self.scraper.get_snapshot()
        if snapshot is None:
            await interaction.followup.send(
                embed=simple_embed(
                    "Stock unavailable",
                    "I couldn't reach the stock page right now. Try again in a minute.",
                    ERROR_COLOR,
                )
            )
            return

        wanted = type.value if type else "both"
        types = list(STOCK_TYPES) if wanted == "both" else [wanted]
        embeds: list[discord.Embed] = []
        for stock_type in types:
            fruits = snapshot.get(stock_type)
            if fruits:
                embeds.append(build_stock_embed(stock_type, fruits, self.emojis))
        if not embeds:
            await interaction.followup.send(
                embed=simple_embed("No stock", "The dealer list is empty right now.", ERROR_COLOR)
            )
            return
        await interaction.followup.send(
            embeds=embeds,
            view=make_invite_view(self.bot.invite_url) or discord.utils.MISSING,
        )

    # ---------------------------------------------------------- /last-seen
    @app_commands.command(
        name="last-seen",
        description="See when fruits were last in stock.",
    )
    @app_commands.describe(fruit="A specific fruit (leave empty to list everything)")
    @app_commands.checks.cooldown(1, 5.0, key=lambda i: (i.guild_id, i.user.id))
    async def last_seen(
        self, interaction: discord.Interaction, fruit: Optional[str] = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        snapshot = await self.scraper.get_snapshot()
        current = snapshot.current_keys() if snapshot else set()

        if fruit:
            found = await self.resolve_fruit(fruit)
            if found is None:
                await interaction.followup.send(
                    embed=simple_embed("Unknown fruit", f"I don't know a fruit called **{fruit}**.", ERROR_COLOR)
                )
                return
            sightings = await self.db.get_last_seen_for(found.key)
            if not sightings:
                await interaction.followup.send(
                    embed=simple_embed(
                        f"Last Seen — {found.name}",
                        "I haven't seen this fruit in stock since I started tracking.",
                    )
                )
                return
            await self.ensure_image(found)
            embed = build_fruit_last_seen_embed(sightings, found, self.emojis, current)
            await interaction.followup.send(embed=embed)
            return

        entries = await self.db.get_last_seen_all()
        pages = build_last_seen_embeds(entries, self.emojis, current)
        if len(pages) == 1:
            await interaction.followup.send(embed=pages[0])
            return
        view = PagedEmbedView(interaction.user.id, pages)
        view.message = await interaction.followup.send(embed=pages[0], view=view, wait=True)

    @last_seen.autocomplete("fruit")
    async def last_seen_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._fruit_choices(current)

    async def _fruit_choices(self, current: str) -> list[app_commands.Choice[str]]:
        names = set(await self.db.list_fruit_names())
        names.update("T-Rex" if k == "trex" else k.title() for k in RARITY_BY_FRUIT if k != "pheonix")
        needle = current.lower().strip()
        matches = sorted(n for n in names if needle in n.lower())
        return [app_commands.Choice(name=n, value=n) for n in matches[:25]]

    # ------------------------------------------------------ /autolast-seen
    @app_commands.command(
        name="autolast-seen",
        description="(Admins) Post the last-seen list in a channel whenever the stock changes.",
    )
    @app_commands.describe(channel="Channel that will receive the last-seen list")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def autolast_seen(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild is not None
        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                embed=simple_embed("No permission", "Only members with **Administrator** can use this.", ERROR_COLOR),
                ephemeral=True,
            )
            return

        missing = missing_channel_perms(channel, interaction.guild.me)
        missing = [m for m in missing if m != "Add Reactions"]
        if missing:
            await interaction.response.send_message(
                embed=simple_embed(
                    "I can't post there",
                    f"I'm missing these permissions in {channel.mention}: **{', '.join(missing)}**.",
                    ERROR_COLOR,
                ),
                ephemeral=True,
            )
            return

        await self.db.set_autolast(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            embed=simple_embed(
                "Auto last-seen enabled",
                f"I'll post the last-seen list in {channel.mention} every time the stock changes.",
                SUCCESS_COLOR,
            ),
            ephemeral=True,
        )

        snapshot = await self.scraper.get_snapshot()
        current = snapshot.current_keys() if snapshot else set()
        entries = [e for e in await self.db.get_last_seen_all() if e.key not in current]
        try:
            await channel.send(embed=build_auto_last_seen_embed(entries, self.emojis))
        except discord.HTTPException as exc:
            log.warning("Initial last-seen post failed: %s", exc)

    @app_commands.command(
        name="autolast-seen-off",
        description="(Admins) Stop the automatic last-seen posts.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def autolast_seen_off(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                embed=simple_embed("No permission", "Only members with **Administrator** can use this.", ERROR_COLOR),
                ephemeral=True,
            )
            return
        removed = await self.db.remove_autolast(interaction.guild.id)
        text = "Auto last-seen posts are now off." if removed else "Auto last-seen wasn't enabled."
        await interaction.response.send_message(
            embed=simple_embed("Auto last-seen", text, SUCCESS_COLOR if removed else EMBED_COLOR),
            ephemeral=True,
        )

    # --------------------------------------------------------------- /stats
    @app_commands.command(
        name="stats",
        description="Price, rarity and picture of a fruit.",
    )
    @app_commands.describe(fruit="The fruit to look up")
    @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
    async def stats(self, interaction: discord.Interaction, fruit: str) -> None:
        await interaction.response.defer(thinking=True)
        found = await self.resolve_fruit(fruit)
        if found is None:
            await interaction.followup.send(
                embed=simple_embed("Unknown fruit", f"I don't know a fruit called **{fruit}**.", ERROR_COLOR)
            )
            return

        snapshot = await self.scraper.get_snapshot()
        in_stock_in: list[str] = []
        if snapshot:
            for stock_type in STOCK_TYPES:
                for live in snapshot.get(stock_type) or []:
                    if live.key == found.key:
                        in_stock_in.append(stock_type)
                        # prefer live details (price/image) over stored ones
                        found = live
        await self.ensure_image(found)
        sightings = await self.db.get_last_seen_for(found.key)
        await interaction.followup.send(
            embed=build_stats_embed(found, sightings, in_stock_in, self.emojis)
        )

    @stats.autocomplete("fruit")
    async def stats_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._fruit_choices(current)

    # --------------------------------------------------------------- /alerts
    @app_commands.command(
        name="alerts",
        description="Show this server's stock alert setup.",
    )
    @app_commands.guild_only()
    async def alerts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        rows = await self.db.get_guild_alerts(interaction.guild.id)
        autolast = await self.db.get_guild_autolast(interaction.guild.id)
        lines: list[str] = []
        by_type = {r["stock_type"]: r for r in rows}
        for stock_type, info in STOCK_TYPES.items():
            row = by_type.get(stock_type)
            if row is None:
                lines.append(f"**{info.label}:** not set up")
            else:
                role = f"<@&{row['role_id']}>" if row["role_id"] else "no role"
                lines.append(f"**{info.label}:** <#{row['channel_id']}> • {role}")
        lines.append(
            f"**Auto last-seen:** <#{autolast['channel_id']}>" if autolast else "**Auto last-seen:** off"
        )
        await interaction.response.send_message(
            embed=simple_embed("Server setup", "\n".join(lines)), ephemeral=True
        )

    # ----------------------------------------------------------- /timepassed
    @app_commands.command(
        name="timepassed",
        description="See how long the bot has been online.",
    )
    async def timepassed(self, interaction: discord.Interaction) -> None:
        started = int(self.bot.started_at)
        uptime = format_duration(time.time() - self.bot.started_at)
        servers, users = self.bot.counts()
        embed = discord.Embed(title="Time passed", color=EMBED_COLOR)
        embed.add_field(name="Online since", value=ts_full(started), inline=False)
        embed.add_field(name="Uptime", value=f"`{uptime}`", inline=True)
        embed.add_field(name="Servers", value=f"`{servers:,}`", inline=True)
        embed.add_field(name="Users", value=f"`{users:,}`", inline=True)
        if self.last_check_at:
            embed.add_field(
                name="Last stock check",
                value=f"<t:{int(self.last_check_at)}:R> • {self.checks_done:,} checks, {self.changes_seen:,} changes",
                inline=False,
            )
        await interaction.response.send_message(embed=embed)

    # ----------------------------------------------------------------- /ping
    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=simple_embed("Pong", f"Gateway latency: `{round(self.bot.latency * 1000)} ms`"),
            ephemeral=True,
        )

    # ----------------------------------------------------------------- /help
    @app_commands.command(name="help", description="List everything the bot can do.")
    async def help(self, interaction: discord.Interaction) -> None:
        text = (
            "**/stockalert** `channel` `role` `type` — auto-post Normal or Mirage stock\n"
            "**/stockalert-remove** `type` — stop an auto-post\n"
            "**/stock** — show the current stock (does not auto-post)\n"
            "**/last-seen** `[fruit]` — when fruits were last in stock\n"
            "**/autolast-seen** `channel` — (admins) auto-post last-seen on every change\n"
            "**/autolast-seen-off** — (admins) turn that off\n"
            "**/stats** `fruit` — price, rarity, type and picture\n"
            "**/alerts** — this server's setup\n"
            "**/timepassed** — how long the bot has been online\n"
            "**/ping** — latency\n\n"
            "Every stock post gets a 🇼 and 🇱 reaction. Legendary and Mythical fruits ping the role."
        )
        embed = simple_embed("Voltix Stock Alerts", text)
        await interaction.response.send_message(
            embed=embed,
            view=make_invite_view(self.bot.invite_url) or discord.utils.MISSING,
            ephemeral=True,
        )


# ============================================================================
# 13. ENTRY POINT
# ============================================================================

def main() -> None:
    setup_logging()
    if not TOKEN:
        log.critical("DISCORD_TOKEN is not set. Add it to your host's environment variables.")
        sys.exit(1)
    if sys.version_info < (3, 10):
        log.critical("Python 3.10+ is required.")
        sys.exit(1)

    bot = VoltixBot()
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.critical("Login failed: the DISCORD_TOKEN is invalid.")
        sys.exit(1)


if __name__ == "__main__":
    main()
