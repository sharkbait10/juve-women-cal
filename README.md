# Juventus Women — full-competition calendar feed

An auto-updating `.ics` feed covering **every** Juventus Women fixture, not
just Serie A and the Champions League. Serie A Women, Women's Champions
League, Coppa Italia Femminile, Serie A Women's Cup and the Supercoppa all
land in one calendar, with venue and broadcaster where they're known.

Built for the annoying gap that ready-made feeds leave: most of them index
the two big competitions and silently skip the cups.

---

## Setup

**1. Create the repo.** Push these files to a new GitHub repository.

**2. Turn on Pages.** Settings → Pages → Source: *Deploy from a branch*,
branch `main`, folder `/docs`. GitHub then serves the feed at:

```
https://YOUR-USERNAME.github.io/YOUR-REPO/juventus-women.ics
```

**3. Run it once by hand.** Actions tab → *Update calendar* → *Run workflow*.
Check that `docs/juventus-women.ics` gets committed. After that it runs on its
own twice a day.

**4. (Recommended) Add a TheSportsDB key.** Without it you get Serie A and the
Champions League only — the same gap you started with. With it you also get the
cups and venue names. Register free at
[thesportsdb.com](https://www.thesportsdb.com/), then add it under
Settings → Secrets and variables → Actions → New repository secret, named
`SPORTSDB_KEY`.

The public test key `3` is deliberately throttled — it returns one event
instead of the full list — so it won't work here.

**5. Subscribe.**

- **Google Calendar** (desktop only): calendar.google.com → `+` next to *Other
  calendars* → *From URL* → paste the URL above.
- **Apple Calendar**: File → New Calendar Subscription.
- **Outlook**: Add calendar → Subscribe from web.

---

## Running locally

```bash
pip install -r requirements.txt
python build_calendar.py --dry-run --verbose   # look, write nothing
python build_calendar.py                       # write docs/juventus-women.ics

export SPORTSDB_KEY=your_key_here              # to include the cups
```

---

## How it works

Three sources are merged, ordered least- to most-trusted, so a later source
corrects an earlier one field by field:

| Source | Covers | Key needed |
|---|---|---|
| **TheSportsDB** | all competitions, plus venue names | free key |
| **fixtur.es ICS** | Serie A Women + Women's Champions League, with kickoff times | none |
| **overrides.yml** | whatever you type in | none |

Then `broadcast.py` enriches near-term fixtures with TV/streaming info and
any kickoff time it can find (see below).

Fixtures are merged **by date**, because a club never plays twice in one day.
This matters: two sources genuinely disagreed about the 22 September 2026
Champions League opponent (one said Benfica, one said Lyon — the club's own
announcement says Benfica). Keying on date collapses that into a single event
that the more trusted source fixes, rather than showing you two phantom games.

A fixture with no announced kickoff becomes an **all-day event** rather than
being dumped at midnight, and the title says `time TBD`. When the time is
confirmed, the next build turns it into a timed event in place — same `UID`,
so your calendar updates the existing entry instead of adding a duplicate.

---

## Broadcast info

**There is no machine-readable source for this.** No API exposes who is
showing a given Serie A Women or Women's Cup match, so it's rule-based:
`config.yml` maps each competition to its usual broadcaster, and
`overrides.yml` handles one-off exceptions (a game moved to a different
channel, a free YouTube stream).

The defaults shipped in `config.yml` are a starting point and **should be
checked against the current season's announcement** — Italian and European
women's rights move between broadcasters regularly, sometimes per-match.
When the FIGC announces that a specific round is on Sky and NOW, or a game
goes out free on the federation's YouTube channel, add it under
`tv_overrides`.

---

## Adding a fixture by hand

Cup draws take days to reach the data feeds. When a game is drawn, put it in
`overrides.yml`:

```yaml
matches:
  - date: "2026-09-12"
    opponent: "Inter"
    home: true
    competition: "Serie A Women's Cup"
    round: "Semifinal"
    time: "18:00"          # local Italian time; omit if not announced
    venue: "Stadio Comunale Vittorio Pozzo-La Marmora, Biella"
    tv: ["Sky Sport", "NOW"]
```

Pushing that file triggers a rebuild immediately. Once the feeds catch up you
can delete the entry — or leave it, since it just keeps overriding with the
same values.

---

## Known limitations

- **Google Calendar refreshes external feeds on its own schedule**, often
  every 8–24 hours and not on demand. A kickoff time confirmed this morning
  may not show in Google until tomorrow, however often this script runs. Apple
  Calendar and Outlook honour the 12-hour refresh hint far better. Nothing in
  this repo can change that; it's Google's polling behaviour.
- **Serie A league kickoff times arrive late.** The upstream feed carries the
  date long before the time, which is why so many league games show as
  all-day at first.
- **Scrapers rot.** If `fixtur.es` changes its title format the parser will
  need a tweak; the script deliberately fails loudly rather than writing an
  empty feed. If every source returns nothing, it exits non-zero and leaves
  the previous `.ics` untouched.
- **Team-name spellings** vary between sources. `team_aliases` in
  `config.yml` normalises them; add any new opponent that shows up twice
  under two names.

## Why not SofaScore?

It was tried. `api.sofascore.com` and `www.sofascore.com` both return **HTTP
403 to every request**, including `robots.txt`, with full browser headers --
Cloudflare bot protection rejecting datacenter IPs. GitHub Actions runners are
datacenter IPs, so it would fail there too, and getting around it would mean
deliberately circumventing an access control. Not a foundation to build on.

## Why not scrape juventus.com or figc.it directly for fixtures?

Both were investigated. The club's own fixture page server-renders only the
next three matches with no times or venues; the full list comes from an
internal JSON endpoint (`/it/api/v1/matcheslist/team-first-team-women`) whose
`KickOffDateTime` and `Venue` fields are mostly `null`, because the site fills
them in client-side from a paid Opta feed using a credential embedded in the
page. Using someone else's licensed feed key isn't something to build on.
FIGC's site is Next.js but loads fixtures client-side too, so there's no
embedded JSON to read.

Hence the source stack above. If you ever want authoritative data, the honest
answer is a paid API key of your own (api-football and similar cover Serie A
Femminile), and the source layer here is easy to extend.
