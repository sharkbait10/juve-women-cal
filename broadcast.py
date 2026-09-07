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
    ("Sky Sport", r"\bSky\s*Sport\b"),
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
]

# "in chiaro" = free-to-air; worth surfacing since it means no subscription.
FREE_MARKERS = [r"\bin chiaro\b", r"\bgratuitamente\b", r"\bgratis\b"]

KICKOFF_RE = re.compile(
    r"(?:ore|alle)\s+(?P<h>[0-2]?\d)[.:](?P<m>[0-5]\d)", re.IGNORECASE
)

DOVE_VEDERE_RE = re.compile(r"dove\s+veder|streaming|diretta\s+tv", re.IGNORECASE)


@dataclass
class BroadcastInfo:
    channels: list[str]
    source_url: str
    source_name: str
    kickoff_local: str | None = None  # "HH:MM" if the article states it
    free_to_air: bool = False

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
    session: requests.Session, home: str, away: str, competition: str, verbose=False
) -> BroadcastInfo | None:
    for url in official_article_urls(home, away, competition):
        try:
            resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        body, _ = extract_article_body(resp.text)
        if not body:
            continue
        if not DOVE_VEDERE_RE.search(body):
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
    window_days: int = 6,
) -> BroadcastInfo | None:
    """Pick the article that refers to this fixture.

    Requires the opponent's name in the title and a publication date within a
    few days before kickoff -- these previews go up shortly beforehand, and
    the date guard stops last season's identical fixture from matching.
    """
    try:
        kickoff = datetime.strptime(match_date, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None

    # Match on the distinctive word of the opponent's name, accent-free.
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
        if art.published:
            delta_days = (kickoff.date() - art.published.date()).days
            if not (-2 <= delta_days <= window_days):
                continue
            score = abs(delta_days)
        else:
            score = 99
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

    resolved: dict = {}
    for m in candidates:
        info = None
        if cfg.get("use_official", True):
            info = resolve_official(session, m.home, m.away, m.competition, verbose)
        if info is None:
            info = match_article(articles, m.opponent, m.date)
        if info:
            resolved[m.key] = info
            if verbose:
                extra = f" kickoff {info.kickoff_local}" if info.kickoff_local else ""
                print(
                    f"      {m.date} vs {m.opponent}: {info.describe()} "
                    f"[{info.source_name}]{extra}"
                )
        elif verbose:
            print(f"      {m.date} vs {m.opponent}: nothing found")

    return resolved
