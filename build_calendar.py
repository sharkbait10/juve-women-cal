#!/usr/bin/env python3
"""
Build an auto-updating iCalendar feed of every Juventus Women fixture.

Merges several sources so that ALL competitions are covered:

  1. fixtur.es ICS feed  -> Serie A Women + Women's Champions League (keyless)
  2. TheSportsDB API     -> adds Coppa Italia Femminile, Serie A Women's Cup,
                            and venue names (needs a free API key)
  3. overrides.yml       -> manually added / corrected fixtures, and the
                            broadcast info that no machine-readable source has

Output: docs/juventus-women.ics  (served by GitHub Pages, subscribed to in
Google Calendar / Apple Calendar / Outlook)

Usage:
    python build_calendar.py                 # normal build
    python build_calendar.py --dry-run       # print what it found, write nothing
    python build_calendar.py --verbose
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import requests
import yaml

import broadcast

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yml"
OVERRIDES_PATH = ROOT / "overrides.yml"
OUT_PATH = ROOT / "docs" / "juventus-women.ics"

USER_AGENT = (
    "juve-women-calendar/1.0 "
    "(personal calendar builder; https://github.com/YOUR-USERNAME/juve-women-cal)"
)
TIMEOUT = 30

# Default match length, used to compute DTEND when we only know kickoff.
MATCH_MINUTES = 115


# --------------------------------------------------------------------------
# model
# --------------------------------------------------------------------------


@dataclass
class Match:
    """One fixture, normalised across all sources."""

    date: str  # YYYY-MM-DD (local Italian date)
    home: str
    away: str
    competition: str
    start: datetime | None = None  # tz-aware UTC; None => time not announced
    venue: str | None = None
    round_name: str | None = None
    score: str | None = None
    status: str | None = None  # e.g. "FT", "postponed"
    broadcasters: list[str] = field(default_factory=list)
    broadcast_source: str | None = None
    broadcast_url: str | None = None
    free_to_air: bool = False
    time_source: str | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def is_home(self) -> bool:
        return _is_juve(self.home)

    @property
    def opponent(self) -> str:
        return self.away if self.is_home else self.home

    @property
    def key(self) -> str:
        """Identity used to merge the same fixture across sources.

        The date alone. A club never plays twice on the same day, so this is
        safe -- and it is deliberately not keyed on the opponent or kickoff
        time, because those are exactly the fields sources disagree on. (Two
        feeds genuinely disagreed about the 22 Sep 2026 Champions League
        opponent.) Keying on the date collapses such a conflict into one
        event that the most-trusted source corrects, instead of two events.
        """
        return self.date

    @property
    def uid(self) -> str:
        digest = hashlib.sha1(self.key.encode()).hexdigest()[:16]
        return f"{digest}@juve-women-cal"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


JUVE_ALIASES = {"juventus", "juventuswomen", "juventusfemminile", "juvewomen"}


def _is_juve(name: str) -> bool:
    return _slug(name) in JUVE_ALIASES


def _canonical_team(name: str, aliases: dict[str, str]) -> str:
    """Map the many spellings of a club onto one display name."""
    slug = _slug(name)
    for canonical, variants in aliases.items():
        if slug == _slug(canonical) or slug in {_slug(v) for v in variants}:
            return canonical
    return name.strip()


# --------------------------------------------------------------------------
# source 1: fixtur.es ICS  (Serie A Women + Women's Champions League)
# --------------------------------------------------------------------------


def _unfold_ics(text: str) -> list[str]:
    """RFC 5545 line unfolding: continuation lines start with space or tab."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def parse_ics(text: str) -> list[dict[str, str]]:
    """Very small VEVENT parser -- enough for these feeds, no dependency."""
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            name, value = line.split(":", 1)
            key = name.split(";", 1)[0].upper()
            current[key] = _ics_unescape(value.strip())
            if ";" in name:
                current[key + "_PARAMS"] = name.split(";", 1)[1]
    return events


SUMMARY_RE = re.compile(r"^(?P<home>.+?)\s+-\s+(?P<away>.+?)$")

# Finished games carry the score in parentheses at the very end, e.g.
#   "Juventus Women - Benfica Women [CL] (2-1)"
# so the score has to come off before the competition tag is looked for.
ICS_SCORE_RE = re.compile(r"\s*\((?P<score>\d+\s*[-–]\s*\d+)\)\s*$")

# fixtur.es tags non-league games in the title, e.g.
#   "Juventus Women - OH Leuven Women [CL]"
# An untagged title in this feed means Serie A Women.
ICS_TAG_RE = re.compile(r"\s*\[(?P<tag>[^\]]+)\]\s*$")
ICS_TAG_COMPETITIONS = {
    "CL": "UEFA Women's Champions League",
    "EL": "UEFA Women's Europa Cup",
    "CI": "Coppa Italia Women",
    "SC": "Supercoppa Italiana",
}
ICS_DEFAULT_COMPETITION = "Serie A Women"


def _parse_ics_dt(value: str) -> datetime | None:
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if fmt == "%Y%m%d":
            return None  # date-only: kickoff time not announced
        return dt.replace(tzinfo=timezone.utc)
    return None


def fetch_fixtures_ics(url: str, aliases: dict, verbose: bool = False) -> list[Match]:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    matches: list[Match] = []

    for ev in parse_ics(resp.text):
        summary = ev.get("SUMMARY", "")

        # Peel the title apart from the right: score, then competition tag,
        # then the two team names. Done in this order because a finished game
        # puts the score after the tag.
        score = None
        score_match = ICS_SCORE_RE.search(summary)
        if score_match:
            score = score_match.group("score").replace(" ", "")
            summary = ICS_SCORE_RE.sub("", summary)

        competition = ICS_DEFAULT_COMPETITION
        tag_match = ICS_TAG_RE.search(summary)
        if tag_match:
            tag = tag_match.group("tag").strip()
            competition = ICS_TAG_COMPETITIONS.get(tag.upper(), tag)
            summary = ICS_TAG_RE.sub("", summary)

        m = SUMMARY_RE.match(summary)
        if not m:
            continue
        home = _canonical_team(m.group("home"), aliases)
        away = _canonical_team(m.group("away"), aliases)
        if not (_is_juve(home) or _is_juve(away)):
            continue

        dtstart_raw = ev.get("DTSTART", "")
        start = _parse_ics_dt(dtstart_raw)
        date = (
            start.astimezone(_rome()).strftime("%Y-%m-%d")
            if start
            else f"{dtstart_raw[0:4]}-{dtstart_raw[4:6]}-{dtstart_raw[6:8]}"
        )

        matches.append(
            Match(
                date=date,
                home=home,
                away=away,
                competition=competition,
                start=start,
                score=score,
                sources=["fixtur.es"],
            )
        )

    if verbose:
        print(f"  fixtur.es: {len(matches)} Juventus Women fixtures")
    return matches


# --------------------------------------------------------------------------
# source 2: TheSportsDB  (adds the cup competitions + venues)
# --------------------------------------------------------------------------


def _sportsdb_get(key: str, path: str, params: dict) -> dict:
    url = f"https://www.thesportsdb.com/api/v1/json/{key}/{path}"
    resp = requests.get(
        url, params=params, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json() or {}


def fetch_sportsdb(cfg: dict, aliases: dict, verbose: bool = False) -> list[Match]:
    key = os.environ.get("SPORTSDB_KEY", "").strip() or cfg.get("api_key", "")
    team_id = str(cfg.get("team_id", ""))
    if not key or not team_id:
        if verbose:
            print("  TheSportsDB: skipped (no SPORTSDB_KEY set)")
        return []

    found: dict[str, Match] = {}

    # Upcoming + recent fixtures for the team, across every competition.
    for path, container in (
        ("eventsnext.php", "events"),
        ("eventslast.php", "results"),
    ):
        try:
            data = _sportsdb_get(key, path, {"id": team_id})
        except requests.RequestException as exc:
            print(f"  TheSportsDB {path} failed: {exc}", file=sys.stderr)
            continue
        for ev in data.get(container) or []:
            match = _sportsdb_event_to_match(ev, aliases)
            if match:
                found[match.key] = match

    # Season-wide pulls, so the whole campaign is present and not just the
    # next few games. Season labels differ per competition, hence config.
    for comp in cfg.get("leagues", []):
        league_id = str(comp.get("id"))
        for season in comp.get("seasons", []):
            try:
                data = _sportsdb_get(
                    key, "eventsseason.php", {"id": league_id, "s": season}
                )
            except requests.RequestException as exc:
                print(f"  TheSportsDB season {league_id}/{season}: {exc}", file=sys.stderr)
                continue
            for ev in data.get("events") or []:
                if team_id not in (
                    str(ev.get("idHomeTeam")),
                    str(ev.get("idAwayTeam")),
                ):
                    continue
                match = _sportsdb_event_to_match(ev, aliases)
                if match:
                    found.setdefault(match.key, match)

    if verbose:
        print(f"  TheSportsDB: {len(found)} fixtures")
    return list(found.values())


def _sportsdb_event_to_match(ev: dict, aliases: dict) -> Match | None:
    home = _canonical_team(ev.get("strHomeTeam") or "", aliases)
    away = _canonical_team(ev.get("strAwayTeam") or "", aliases)
    if not home or not away:
        return None
    if not (_is_juve(home) or _is_juve(away)):
        return None

    start = None
    ts = ev.get("strTimestamp")
    if ts:
        try:
            start = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except ValueError:
            start = None
    # A 00:00:00 kickoff means "time not announced yet", not midnight.
    if start and ev.get("strTime") in (None, "", "00:00:00"):
        start = None

    date = ev.get("dateEventLocal") or ev.get("dateEvent")
    if not date:
        return None

    hs, as_ = ev.get("intHomeScore"), ev.get("intAwayScore")
    score = f"{hs}-{as_}" if hs not in (None, "") and as_ not in (None, "") else None

    rnd = ev.get("intRound")
    return Match(
        date=date,
        home=home,
        away=away,
        competition=(ev.get("strLeague") or "").strip(),
        start=start,
        venue=(ev.get("strVenue") or "").strip() or None,
        round_name=f"Round {rnd}" if rnd and str(rnd) not in ("0",) else None,
        score=score,
        status=(ev.get("strStatus") or "").strip() or None,
        sources=["TheSportsDB"],
    )


# --------------------------------------------------------------------------
# source 3: overrides.yml  (manual fixtures + corrections)
# --------------------------------------------------------------------------


def load_manual(overrides: dict, aliases: dict, verbose: bool = False) -> list[Match]:
    matches: list[Match] = []
    for item in overrides.get("matches", []) or []:
        date = str(item.get("date", "")).strip()
        opponent = str(item.get("opponent", "")).strip()
        if not date or not opponent:
            continue
        opponent = _canonical_team(opponent, aliases)
        at_home = bool(item.get("home", True))
        home, away = ("Juventus", opponent) if at_home else (opponent, "Juventus")

        start = None
        if item.get("time"):
            local = datetime.strptime(f"{date} {item['time']}", "%Y-%m-%d %H:%M")
            start = local.replace(tzinfo=_rome()).astimezone(timezone.utc)

        matches.append(
            Match(
                date=date,
                home=home,
                away=away,
                competition=str(item.get("competition", "")).strip(),
                start=start,
                venue=(str(item.get("venue")).strip() if item.get("venue") else None),
                round_name=(
                    str(item.get("round")).strip() if item.get("round") else None
                ),
                status=(str(item.get("status")).strip() if item.get("status") else None),
                broadcasters=list(item.get("tv", []) or []),
                sources=["overrides.yml"],
            )
        )
    if verbose:
        print(f"  overrides.yml: {len(matches)} manual fixtures")
    return matches


# --------------------------------------------------------------------------
# merging
# --------------------------------------------------------------------------


def merge(groups: list[list[Match]], verbose: bool = False) -> list[Match]:
    """Merge sources by fixture identity.

    Later groups win on conflicts, so order them least- to most-trusted.
    Fields are filled in individually: a source that knows the venue but not
    the kickoff still contributes the venue.
    """
    merged: dict[str, Match] = {}
    for group in groups:
        for incoming in group:
            existing = merged.get(incoming.key)
            if existing is None:
                merged[incoming.key] = incoming
                continue
            for attr in (
                "home",
                "away",
                "competition",
                "venue",
                "round_name",
                "score",
                "status",
                "start",
            ):
                new_value = getattr(incoming, attr)
                if new_value not in (None, ""):
                    setattr(existing, attr, new_value)
            if incoming.broadcasters:
                existing.broadcasters = incoming.broadcasters
            existing.sources = sorted(set(existing.sources) | set(incoming.sources))

    out = sorted(merged.values(), key=lambda m: (m.date, m.start or _MAX_DT))
    if verbose:
        print(f"  merged: {len(out)} unique fixtures")
    return out


_MAX_DT = datetime(2100, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# broadcast enrichment
# --------------------------------------------------------------------------


def apply_broadcasters(
    matches: list[Match],
    cfg: dict,
    overrides: dict,
    resolved: dict | None = None,
) -> int:
    """Attach TV / streaming info, and backfill kickoff times from articles.

    Precedence, weakest first:
      1. per-competition default from config.yml   (a guess)
      2. per-match info scraped from articles      (what actually got announced)
      3. tv_overrides in overrides.yml             (your manual word, final)

    Returns how many kickoff times were recovered from articles, which is the
    main reason to run this daily: fixtures are published with a date long
    before a time, and the "dove vederla" preview is usually where the time
    shows up first.
    """
    rules = cfg.get("broadcasters", {}) or {}
    resolved = resolved or {}
    # Keyed on date, same as Match.key. The `opponent` field in the YAML is
    # kept purely so the file stays readable.
    per_match = {
        str(o.get("date", "")).strip(): list(o.get("tv", []) or [])
        for o in (overrides.get("tv_overrides", []) or [])
    }

    times_recovered = 0
    for match in matches:
        manual = bool(match.broadcasters)  # came straight from overrides.yml

        # 1. competition-level default
        if not manual:
            for comp_key, channels in rules.items():
                if comp_key.lower() in (match.competition or "").lower():
                    match.broadcasters = list(channels)
                    match.broadcast_source = "competition default"
                    break

        # 2. per-match article, if we found one
        info = resolved.get(match.key)
        if info and not manual:
            match.broadcasters = list(info.channels)
            match.broadcast_source = info.source_name
            match.broadcast_url = info.source_url
            match.free_to_air = info.free_to_air

        # Kickoff backfill happens even when the channel was set manually --
        # the time is useful regardless of who is showing it.
        if info and info.kickoff_local and match.start is None:
            try:
                local = datetime.strptime(
                    f"{match.date} {info.kickoff_local}", "%Y-%m-%d %H:%M"
                )
            except ValueError:
                local = None
            if local:
                match.start = local.replace(tzinfo=_rome()).astimezone(timezone.utc)
                match.time_source = info.source_name
                times_recovered += 1

        # 3. explicit manual override always wins
        explicit = per_match.get(match.key)
        if explicit:
            match.broadcasters = explicit
            match.broadcast_source = "overrides.yml"

    return times_recovered


# --------------------------------------------------------------------------
# ICS output
# --------------------------------------------------------------------------


def _rome() -> timezone:
    """Europe/Rome without a tzdata dependency: CEST Mar-Oct, CET otherwise.

    Only used for display dates and for reading local times out of
    overrides.yml, so the DST edge cases are not load-bearing.
    """
    month = datetime.now(timezone.utc).month
    return timezone(timedelta(hours=2 if 3 <= month <= 10 else 1))


def ics_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """RFC 5545: lines must be <=75 octets, continuations start with a space."""
    out, current = [], ""
    for char in line:
        if len(current.encode()) + len(char.encode()) > 73:
            out.append(current)
            current = " " + char
        else:
            current += char
    out.append(current)
    return "\r\n".join(out)


def build_ics(matches: list[Match], cfg: dict) -> str:
    name = cfg.get("calendar_name", "Juventus Women")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//juve-women-cal//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{ics_escape(name)}",
        f"X-WR-CALDESC:{ics_escape(cfg.get('calendar_description', ''))}",
        "X-PUBLISHED-TTL:PT12H",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
    ]

    for match in matches:
        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{match.uid}")
        lines.append(f"DTSTAMP:{stamp}")
        lines.append(f"SEQUENCE:{_sequence(match)}")

        if match.start:
            end = match.start + timedelta(minutes=MATCH_MINUTES)
            lines.append(f"DTSTART:{match.start.strftime('%Y%m%dT%H%M%SZ')}")
            lines.append(f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}")
        else:
            # Kickoff not announced -> all-day event so it is still visible.
            day = datetime.strptime(match.date, "%Y-%m-%d")
            nxt = day + timedelta(days=1)
            lines.append(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{nxt.strftime('%Y%m%d')}")

        lines.append(f"SUMMARY:{ics_escape(_summary(match, cfg))}")
        if match.venue:
            lines.append(f"LOCATION:{ics_escape(match.venue)}")
        lines.append(f"DESCRIPTION:{ics_escape(_description(match))}")
        lines.append("STATUS:CANCELLED" if _is_cancelled(match) else "STATUS:CONFIRMED")
        lines.append("TRANSP:TRANSPARENT")
        if match.competition:
            lines.append(f"CATEGORIES:{ics_escape(match.competition)}")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def _sequence(match: Match) -> int:
    """Bump when kickoff details change so clients refresh the event."""
    basis = f"{match.start}|{match.venue}|{match.status}"
    return int(hashlib.sha1(basis.encode()).hexdigest()[:4], 16) % 1000


def _is_cancelled(match: Match) -> bool:
    return (match.status or "").lower() in {"postponed", "cancelled", "canceled"}


def _summary(match: Match, cfg: dict) -> str:
    prefix = cfg.get("event_prefix", "")
    home_mark = cfg.get("home_marker", "(H)")
    away_mark = cfg.get("away_marker", "(A)")
    marker = home_mark if match.is_home else away_mark

    label = f"{prefix}{match.home} – {match.away}".strip()
    if match.score:
        label += f" [{match.score}]"
    parts = [label, marker]
    if match.competition:
        parts.append(f"· {_short_competition(match.competition)}")
    if not match.start:
        parts.append("· time TBD")
    return " ".join(parts)


SHORT_NAMES = {
    "uefa womens champions league": "UWCL",
    "womens champions league": "UWCL",
    "italy serie a women": "Serie A Women",
    "serie a women": "Serie A Women",
    "italian serie a womens cup": "Women's Cup",
    "serie a womens cup": "Women's Cup",
    "coppa italia women": "Coppa Italia",
}


def _short_competition(name: str) -> str:
    return SHORT_NAMES.get(name.lower().replace("'", ""), name)


def _description(match: Match) -> str:
    rows = []
    if match.competition:
        rows.append(f"Competition: {match.competition}")
    if match.round_name:
        rows.append(match.round_name)
    rows.append(f"Venue: {match.venue}" if match.venue else "Venue: TBC")
    if match.broadcasters:
        line = "TV / streaming: " + ", ".join(match.broadcasters)
        if match.free_to_air:
            line += " (free-to-air)"
        rows.append(line)
        if match.broadcast_source and match.broadcast_source != "competition default":
            rows.append(f"  confirmed by: {match.broadcast_source}")
        elif match.broadcast_source == "competition default":
            rows.append("  (usual broadcaster for this competition -- unconfirmed)")
        if match.broadcast_url:
            rows.append(f"  {match.broadcast_url}")
    else:
        rows.append("TV / streaming: not announced")
    if match.score:
        rows.append(f"Result: {match.score}")
    if not match.start:
        rows.append("Kickoff time not yet announced -- shown as all-day.")
    elif match.time_source:
        rows.append(f"Kickoff time from: {match.time_source}")
    rows.append("")
    rows.append("Sources: " + ", ".join(match.sources))
    rows.append(
        "Updated: " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    )
    return "\n".join(rows)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--no-broadcast",
        action="store_true",
        help="skip the TV/streaming lookup (faster, fewer requests)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    cfg = load_yaml(CONFIG_PATH)
    overrides = load_yaml(OVERRIDES_PATH)
    aliases = cfg.get("team_aliases", {}) or {}

    print("Fetching sources...")
    groups: list[list[Match]] = []

    # Ordered least- to most-trusted; later sources correct earlier ones.
    # TheSportsDB goes first because it is the only source for the cup
    # competitions and for venue names, but it has been observed to get
    # Champions League opponents wrong. It still contributes the venue,
    # since a later source only overwrites fields it actually knows.
    try:
        groups.append(
            fetch_sportsdb(cfg.get("thesportsdb", {}) or {}, aliases, args.verbose)
        )
    except requests.RequestException as exc:
        print(f"  TheSportsDB failed: {exc}", file=sys.stderr)

    ics_url = (cfg.get("fixtures_ics") or {}).get("url")
    if ics_url:
        try:
            groups.append(fetch_fixtures_ics(ics_url, aliases, args.verbose))
        except requests.RequestException as exc:
            print(f"  fixtur.es failed: {exc}", file=sys.stderr)

    groups.append(load_manual(overrides, aliases, args.verbose))

    if not any(groups):
        print("No fixtures from any source -- refusing to overwrite the feed.", file=sys.stderr)
        return 1

    matches = merge(groups, args.verbose)

    resolved = {}
    broadcast_cfg = cfg.get("broadcast_lookup", {}) or {}
    if broadcast_cfg.get("enabled", True) and not args.no_broadcast:
        try:
            resolved = broadcast.resolve_all(
                matches, broadcast_cfg, USER_AGENT, args.verbose
            )
        except Exception as exc:  # never let enrichment break the build
            print(f"  broadcast lookup failed: {exc}", file=sys.stderr)

    times_recovered = apply_broadcasters(matches, cfg, overrides, resolved)
    if times_recovered:
        print(f"recovered {times_recovered} kickoff time(s) from match previews")

    # Trim ancient history so the file stays small.
    keep_from = (
        datetime.now(timezone.utc) - timedelta(days=int(cfg.get("keep_past_days", 400)))
    ).strftime("%Y-%m-%d")
    matches = [m for m in matches if m.date >= keep_from]

    upcoming = [
        m for m in matches if m.date >= datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ]
    print(f"{len(matches)} fixtures ({len(upcoming)} upcoming)")

    if args.verbose or args.dry_run:
        for m in upcoming[:15]:
            when = m.start.strftime("%H:%M UTC") if m.start else "  TBD  "
            tv = ", ".join(m.broadcasters) or "-"
            print(
                f"  {m.date} {when}  {_short_competition(m.competition or '?'):<14} "
                f"{m.home} v {m.away:<22} {tv}"
            )

    missing_comp = [m for m in upcoming if not m.competition]
    if missing_comp:
        print(
            f"note: {len(missing_comp)} upcoming fixtures have no competition label "
            "(enable TheSportsDB or add them to overrides.yml)"
        )

    if args.dry_run:
        print("dry run -- nothing written")
        return 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(build_ics(matches, cfg), encoding="utf-8")
    print(f"wrote {OUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
