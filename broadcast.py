#!/usr/bin/env python3
"""
Per-match TV / streaming resolution.

Replaces guessing-by-competition. Nobody publishes this as structured data,
so this does automatically what you'd do by hand: find the "dove vederla"
article for a specific game and read the broadcaster out of it.

Sources, most authoritative first:

  1. juventus.com  -- official "Dove vedere X-Y" article. The page embeds a
     schema.org NewsArticle whose `articleBody` is clean text containing both
     the broadcaster AND the kickoff time. URL slugs are predictable, so
     candidates are constructed and tried.
  2. RSS feeds of women's-football outlets (lfootball.it, juventusnews24.com).
     WordPress feeds carry the whole article in `content:encoded`, so one
     request gets ~30 articles with no per-page scraping.

Matching an article to a fixture uses opponent name + publication date close
to kickoff, because these previews always go up in the days just before.

Everything is best-effort: no article found means the competition default in
config.yml is used, and the event says "not announced".
"""

from __future__ import annotations

import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

RSS_NS = {"content": "http://purl.org/rss/1.0/modules/content/"}
TIMEOUT = 30

# Ordered longest-first so "Sky Sport" is preferred over a bare "Sky", and so
# "Rai Sport" isn't double-counted as "Rai".
BROADCASTER_PATTERNS: list[tuple[str, str]] = [
    ("DAZN", r"\bDAZN\b"),
    ("Sky Sport", r"\bSky\s*Sport(?:s)?\b"),
    ("Sky Go", r"\bSky\s*Go\b"),
    ("NOW", r"\bNOW\b(?!\w)"),
    ("Rai Sport", r"\bRai\s*Sport\b"),
    ("RaiPlay", r"\bRai\s*Play\b"),
    ("Disney+", r"Disney\s*\+"),
    ("Prime Video", r"\bPrime\s+Video\b"),
    ("Juventus TV", r"\bJuventus\s*TV\b"),
    ("TIMVISION", r"\bTIMVISION\b"),
    ("YouTube", r"\bYouTube\b"),
    ("Twitch", r"\bTwitch\b"),
    ("UEFA.tv", r"\bUEFA\.tv\b"),
    # UK / international, mainly for the UK listings source below.
    ("beIN Sports", r"\bbe\s*IN\s*SPORTS?\b"),
    ("BBC", r"\bBBC\b"),
    ("BBC iPlayer", r"\bBBC\s*iPlayer\b"),
    ("ITV", r"\bITV\b"),
    ("Channel 4", r"\bChannel\s*4\b"),
    ("TNT Sports", r"\bTNT\s*Sports?\b"),
    ("Eurovision Sport", r"\bEurovision\s*Sport\b"),
    ("tabii", r"\btabii\b"),
]

# "in chiaro" = free-to-air; worth surfacing since it means no subscription.
FREE_MARKERS = [r"\bin chiaro\b", r"\bgratuitamente\b", r"\bgratis\b"]

KICKOFF_RE = re.compile(
    r"(?:ore|alle)\s+(?P<h>[0-2]?\d)[.:](?P<m>[0-5]\d)", re.IGNORECASE
)

DOVE_VEDERE_RE = re.compile(r"dove\s+veder|streaming|diretta\s+tv", re.IGNORECASE)

# Articles state the match date in the body, e.g. "Mercoledì 24 settembre
# 2025 la Juventus Women affronterà l'Inter". That date is the only reliable
# way to tell this season's preview from last season's identical fixture --
# publication date alone is not enough, and slugs carry no year at all.
IT_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
IT_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})\s+(?P<month>"
    + "|".join(IT_MONTHS)
    + r")(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)


def find_match_dates(text: str, fallback_year: int | None = None) -> list[str]:
    """Every 'D month [YYYY]' date in the text, as YYYY-MM-DD.

    A missing year is filled from `fallback_year` (the article's publication
    year) rather than assumed to be the current one.
    """
    out: list[str] = []
    for m in IT_DATE_RE.finditer(text):
        month = IT_MONTHS[m.group("month").lower()]
        year = m.group("year")
        if year:
            y = int(year)
        elif fallback_year:
            y = fallback_year
        else:
            continue
        try:
            out.append(f"{y:04d}-{month:02d}-{int(m.group('day')):02d}")
        except ValueError:
            continue
    return out


def article_matches_date(
    body: str,
    match_date: str,
    published: datetime | None,
    max_age_hours: int,
) -> tuple[bool, str]:
    """Decide whether an article really is about the given fixture.

    Two ways to pass:

      1. The body states the fixture's own date -> accepted outright. This is
         the strong signal.
      2. Otherwise, publication must sit close to the match. Articles often
         say "oggi alle 18" instead of a full date, and they routinely mention
         OTHER upcoming dates in passing, so a non-matching stated date can't
         be treated as fatal on its own -- but a year-old article will always
         fail the timing test.

    Timing is measured against the END of the match day, so a preview
    published on the morning of the game counts as recent rather than
    appearing to predate the fixture.
    """
    try:
        day = datetime.strptime(match_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False, "unparseable fixture date"
    reference = day + timedelta(days=1)  # end of match day

    stated = find_match_dates(body, published.year if published else None)
    if match_date in stated:
        return True, "body states this fixture's date"

    if published is None:
        return False, "no matching date in body and no publication date"

    age_hours = (reference - published).total_seconds() / 3600.0
    if age_hours > max_age_hours + 24:
        detail = f", article mentions {stated[0]}" if stated else ""
        return False, (
            f"published {age_hours / 24:.0f} days before the match "
            f"(limit {max_age_hours}h){detail}"
        )
    if age_hours < -24:
        return False, "published after the match"

    return True, "published close to kickoff"


@dataclass
class BroadcastInfo:
    channels: list[str]
    source_url: str
    source_name: str
    kickoff_local: str | None = None  # "HH:MM" if the article states it
    free_to_air: bool = False
    uk_channels: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.uk_channels is None:
            self.uk_channels = []

    def describe(self) -> str:
        text = ", ".join(self.channels)
        if self.free_to_air:
            text += " (free-to-air)"
        return text


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------


def deaccent(text: str) -> str:
    """St. Pölten -> St. Polten, so title matching survives accents."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def strip_html(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def find_broadcasters(text: str) -> list[str]:
    found: list[str] = []
    for label, pattern in BROADCASTER_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE) and label not in found:
            found.append(label)
    return found


def is_free_to_air(text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in FREE_MARKERS)


def find_kickoff(text: str) -> str | None:
    m = KICKOFF_RE.search(text)
    if not m:
        return None
    hour, minute = int(m.group("h")), int(m.group("m"))
    if 0 <= hour <= 23:
        return f"{hour:02d}:{minute:02d}"
    return None


def _slugify(text: str) -> str:
    text = deaccent(text).lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# --------------------------------------------------------------------------
# source 1: official juventus.com article
# --------------------------------------------------------------------------

JUVE_ARTICLE_BASE = "https://www.juventus.com/it/news/articoli/"

# Competition -> the prefix the club uses in these article slugs.
COMPETITION_SLUGS = {
    "serie a women": ["serie-a-women"],
    "serie a women's cup": ["serie-a-women-s-cup", "women-s-cup", "serie-a-women"],
    "uefa women's champions league": [
        "uefa-women-s-champions-league",
        "women-s-champions-league",
        "champions-league",
    ],
    "coppa italia women": ["coppa-italia", "coppa-italia-women"],
    "supercoppa italiana": ["supercoppa", "supercoppa-italiana"],
}


def _short_team_slug(name: str) -> list[str]:
    """Slug variants the club might use for a club name."""
    base = _slugify(name)
    variants = {base}
    # The club writes "Juventus-Milan", not "Juventus Women-Milan Women".
    stripped = re.sub(r"-(women|femminile)$", "", base)
    variants.add(stripped)
    return [v for v in variants if v]


def official_article_urls(home: str, away: str, competition: str) -> list[str]:
    comp_slugs = COMPETITION_SLUGS.get(
        (competition or "").lower().strip(), [_slugify(competition)]
    )
    urls: list[str] = []
    for comp in comp_slugs:
        if not comp:
            continue
        for h in _short_team_slug(home):
            for a in _short_team_slug(away):
                urls.append(f"{JUVE_ARTICLE_BASE}{comp}-dove-vedere-{h}-{a}")
    # De-duplicate, keep order.
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out[:6]  # cap the guessing


LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld[^"]*json"[^>]*>(.*?)</script>', re.S | re.I
)


def extract_article_body(page_html: str) -> tuple[str | None, str | None]:
    """Pull (articleBody, datePublished) out of the embedded schema.org JSON.

    Note the MIME type is HTML-escaped on juventus.com as
    `application/ld&#x2B;json`, hence the loose type match above.
    """
    for block in LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(html.unescape(block) if "&quot;" in block else block)
        except json.JSONDecodeError:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("articleBody"):
                return obj["articleBody"], obj.get("datePublished")
    return None, None


def resolve_official(
    session: requests.Session,
    home: str,
    away: str,
    competition: str,
    match_date: str,
    max_age_hours: int,
    verbose=False,
) -> BroadcastInfo | None:
    """Fetch the club's own preview, but only trust it if it is THIS fixture.

    Slugs contain no year, so `serie-a-women-s-cup-dove-vedere-juventus-inter`
    resolves to whichever season's article exists -- which produced a real bug
    where a 2025 article supplied a kickoff time for a 2026 match. Hence the
    date validation below.
    """
    for url in official_article_urls(home, away, competition):
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        body, published_raw = extract_article_body(resp.text)
        if not body:
            continue
        if not DOVE_VEDERE_RE.search(body):
            continue

        published = None
        if published_raw:
            try:
                published = datetime.fromisoformat(
                    published_raw.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except ValueError:
                published = None

        ok, reason = article_matches_date(body, match_date, published, max_age_hours)
        if not ok:
            if verbose:
                print(f"      rejected {url.rsplit('/', 1)[-1]}: {reason}")
            continue

        channels = find_broadcasters(body)
        if not channels:
            continue
        if verbose:
            print(f"      official: {url}")
        return BroadcastInfo(
            channels=channels,
            source_url=url,
            source_name="juventus.com",
            kickoff_local=find_kickoff(body),
            free_to_air=is_free_to_air(body),
        )
    return None


# --------------------------------------------------------------------------
# source 2: RSS feeds
# --------------------------------------------------------------------------


@dataclass
class Article:
    title: str
    link: str
    published: datetime | None
    body: str
    feed_name: str


def _parse_rss_date(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(value.strip(), fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def fetch_feed(
    session: requests.Session, url: str, name: str, verbose=False
) -> list[Article]:
    try:
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except (requests.RequestException, ET.ParseError) as exc:
        if verbose:
            print(f"    feed {name} unavailable: {exc}")
        return []

    articles: list[Article] = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        if not DOVE_VEDERE_RE.search(title):
            continue  # only interested in the preview/where-to-watch pieces
        body = item.findtext("content:encoded", namespaces=RSS_NS) or item.findtext(
            "description"
        ) or ""
        articles.append(
            Article(
                title=title,
                link=(item.findtext("link") or "").strip(),
                published=_parse_rss_date(item.findtext("pubDate")),
                body=strip_html(body),
                feed_name=name,
            )
        )
    if verbose:
        print(f"    feed {name}: {len(articles)} where-to-watch articles")
    return articles


def match_article(
    articles: list[Article],
    opponent: str,
    match_date: str,
    max_age_hours: int = 48,
    verbose: bool = False,
) -> BroadcastInfo | None:
    """Pick the article that refers to this fixture.

    Requires the opponent in the title AND the same date validation used for
    the official article: a stated match date must agree, and publication
    must be within `max_age_hours` of kickoff. Without this, last season's
    preview of the same fixture matches happily.
    """
    tokens = [
        t
        for t in re.split(r"[^A-Za-z0-9]+", deaccent(opponent).lower())
        if len(t) > 2 and t not in {"women", "femminile", "fcf", "women's"}
    ]
    if not tokens:
        return None

    best: tuple[float, Article] | None = None
    for art in articles:
        title = deaccent(art.title).lower()
        if not any(t in title for t in tokens):
            continue

        ok, reason = article_matches_date(
            art.body, match_date, art.published, max_age_hours
        )
        if not ok:
            if verbose:
                print(f"      rejected {art.feed_name} article: {reason}")
            continue

        score = (
            abs((datetime.strptime(match_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc) - art.published).total_seconds())
            if art.published
            else 1e9
        )
        if best is None or score < best[0]:
            best = (score, art)

    if best is None:
        return None

    art = best[1]
    channels = find_broadcasters(art.body) or find_broadcasters(art.title)
    if not channels:
        return None
    return BroadcastInfo(
        channels=channels,
        source_url=art.link,
        source_name=art.feed_name,
        kickoff_local=find_kickoff(art.body),
        free_to_air=is_free_to_air(art.body),
    )


# --------------------------------------------------------------------------
# source 3: UK TV listings
# --------------------------------------------------------------------------

# The Italian previews only tell you the Italian broadcaster. For UK channels
# (Disney+ carries every UWCL match, but the BBC's free-to-air picks vary game
# by game) a UK guide is needed instead.
#
# Note beIN Sports is NOT a UK broadcaster for the UWCL: beIN holds MENA and
# Asia rights. If you see a match on beIN via IPTV that is a MENA/Asia feed,
# which is fine to watch but is not what a UK listing will show.

UK_TOKEN_RE = re.compile(
    r'class="(fixture-date|fixture__time|fixture__teams|fixture__channel[^"]*)"[^>]*>(.*?)</div>',
    re.S,
)
ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)", re.IGNORECASE)

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}


def _parse_uk_date(label: str) -> str | None:
    """'Tuesday 22nd September 2026' -> '2026-09-22'."""
    cleaned = ORDINAL_RE.sub(r"\1", label or "")
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", cleaned)
    if not m:
        return None
    month = MONTHS.get(m.group(2).lower())
    if not month:
        return None
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"


def parse_uk_listings(page_html: str) -> list[tuple[str, str, list[str]]]:
    """Return [(date, teams, channels)] by walking the page in document order.

    Fixtures sit under a date heading rather than inside it, so the current
    heading has to be tracked as we go -- grouping by container gets the
    dates wrong.
    """
    rows: list[tuple[str, str, list[str]]] = []
    current_date: str | None = None
    pending: dict = {}

    def flush():
        if pending.get("teams") and current_date:
            rows.append((current_date, pending["teams"], pending.get("channels", [])))

    for m in UK_TOKEN_RE.finditer(page_html):
        kind, body = m.group(1), strip_html(m.group(2))
        if kind == "fixture-date":
            flush()
            pending.clear()
            current_date = _parse_uk_date(body)
        elif kind == "fixture__time":
            flush()
            pending.clear()
        elif kind == "fixture__teams":
            pending["teams"] = body
        elif kind.startswith("fixture__channel"):
            found = find_broadcasters(body)
            pending.setdefault("channels", [])
            for ch in found:
                if ch not in pending["channels"]:
                    pending["channels"].append(ch)
    flush()
    return rows


def fetch_uk_listings(
    session: requests.Session, url: str, verbose: bool = False
) -> dict[str, list[str]]:
    """Map 'YYYY-MM-DD|opponent-token' -> UK channels."""
    try:
        resp = session.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        if verbose:
            print(f"    UK listings unavailable: {exc}")
        return {}

    listings: dict[str, list[str]] = {}
    for date, teams, channels in parse_uk_listings(resp.text):
        if not channels:
            continue
        # Only keep fixtures involving Juventus; key on date since the club
        # plays at most once a day.
        if "juventus" in deaccent(teams).lower():
            listings[date] = channels
    if verbose:
        print(f"    UK listings: {len(listings)} Juventus fixtures with channels")
    return listings


# --------------------------------------------------------------------------
# entry point used by build_calendar.py
# --------------------------------------------------------------------------


def build_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"User-Agent": user_agent, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"}
    )
    return session


def resolve_all(
    matches,
    cfg: dict,
    user_agent: str,
    verbose: bool = False,
) -> dict:
    """Resolve broadcast info for upcoming (and just-played) fixtures.

    Returns {match.key: BroadcastInfo}. Only looks ahead a limited window,
    because previews don't exist months in advance -- so there is no point
    firing requests at articles that cannot be there yet.
    """
    lookahead = int(cfg.get("lookahead_days", 12))
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=lookahead)

    candidates = []
    for m in matches:
        try:
            d = datetime.strptime(m.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if today - timedelta(days=2) <= d <= horizon:
            candidates.append(m)

    if not candidates:
        return {}

    session = build_session(user_agent)
    if verbose:
        print(f"  resolving TV info for {len(candidates)} near-term fixtures")

    # One request per feed covers every fixture, so do it up front.
    articles: list[Article] = []
    for feed in cfg.get("feeds", []) or []:
        if feed.get("enabled", True):
            articles += fetch_feed(session, feed["url"], feed.get("name", feed["url"]), verbose)

    # UK channels come from a separate guide; one request covers the whole
    # season, so these are applied to EVERY fixture rather than only the
    # near-term ones the article scraping looks at.
    uk_cfg = cfg.get("uk_listings", {}) or {}
    uk_listings: dict[str, list[str]] = {}
    if uk_cfg.get("enabled") and uk_cfg.get("url"):
        uk_listings = fetch_uk_listings(session, uk_cfg["url"], verbose)

    max_age_hours = int(cfg.get("max_article_age_hours", 48))

    resolved: dict = {}
    for m in candidates:
        info = None
        if cfg.get("use_official", True):
            info = resolve_official(
                session, m.home, m.away, m.competition, m.date, max_age_hours, verbose
            )
        if info is None:
            info = match_article(
                articles, m.opponent, m.date, max_age_hours, verbose
            )
        if info is not None:
            resolved[m.key] = info
            if verbose:
                extra = f" kickoff {info.kickoff_local}" if info.kickoff_local else ""
                print(
                    f"      {m.date} vs {m.opponent}: {info.describe()} "
                    f"[{info.source_name}]{extra}"
                )
        elif verbose:
            print(f"      {m.date} vs {m.opponent}: nothing found")

    # Attach UK channels across the full fixture list.
    for m in matches:
        uk = uk_listings.get(m.date)
        if not uk:
            continue
        info = resolved.get(m.key)
        if info is None:
            info = BroadcastInfo(
                channels=[],
                source_url=uk_cfg.get("url", ""),
                source_name=uk_cfg.get("name", "UK listings"),
            )
            resolved[m.key] = info
        info.uk_channels = uk
        if verbose:
            print(f"      {m.date} vs {m.opponent}: UK -> {', '.join(uk)}")

    return resolved
