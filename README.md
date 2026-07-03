# Wheelie Fresh Bins → calendar feed

A daily GitHub Actions job logs into the [Wheelie Fresh Bins customer
portal](https://portal.wheeliefreshbins.com/), reads your bin cleaning
schedule, and **publishes it to GitHub Pages** as `bin-clean.ics`. Calendar
apps subscribe to that public URL and stay up to date automatically. The
file is regenerated and redeployed each day — it is never committed into
the repo.

The calendar contains only all-day "Bin clean" events (with a reminder the
evening before) — **no address or personal details**. Ad-hoc extra cleans
are included; Bin Holidays recorded on the portal are excluded.

## Publishing model: GitHub Pages, not raw files

The schedule is served at:

```
https://<user>.github.io/<repo>/bin-clean.ics
```

for this repo that is `https://richardthe3rd.github.io/wheeliebinfresh-ics/bin-clean.ics`.

Why Pages and not the raw file in the repo? A calendar app needs a URL it
can fetch **without logging in**. A private repo's `raw.githubusercontent.com`
URL requires a token, so calendar apps can't use it. GitHub Pages gives a
clean public URL.

> **The repo must be public** for Pages on a free GitHub plan (private-repo
> Pages needs a paid plan). The ICS holds only dates, no address, so making
> the repo public is safe. If you'd rather keep it private, you need GitHub
> Pro/Team, otherwise the feed can't be subscribed to.

## One-time setup (all doable from a phone)

### 1. Add the portal credentials as secrets

Repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**, twice:

- `WFB_EMAIL` — the email you log into the portal with.
- `WFB_PASSWORD` — your portal password.

(Your booking number isn't a secret: it's read from your authenticated
account page and only ever leaves the program as a one-way hash in the
event IDs, so the raw number never appears in the public calendar.)

### 2. Turn on GitHub Pages (source: GitHub Actions)

Repo → **Settings** → **Pages** → under **Build and deployment**, set
**Source** to **GitHub Actions**. (No branch to pick — the workflow deploys
the file directly.)

### 3. Run the workflow once

**Actions** tab → **Publish bin-clean.ics to Pages** → **Run workflow**.
When it goes green, open the run's **deploy** step (or Settings → Pages) to
get your `…github.io/…/bin-clean.ics` URL. If it goes red, the log says why
(bad credentials, portal/layout change) and GitHub emails you.

It then runs by itself every day at about 06:00 UTC.

### 4. Subscribe in your calendar

**Google Calendar** (must be done on the web, not the app):

1. [calendar.google.com](https://calendar.google.com) → **Settings** (gear)
   → **Add calendar** → **From URL**.
2. Paste the Pages URL → **Add calendar**. It syncs to the phone app;
   Google refreshes subscribed calendars every several hours up to ~a day.

**Apple Calendar (iPhone/iPad):**

1. **Settings** → **Apps** → **Calendar** → **Calendar Accounts** →
   **Add Account** → **Other** → **Add Subscribed Calendar**.
2. Paste the Pages URL → **Next** → **Save**.

## Note on the default branch

This repo currently has no `main` branch — the default branch is the one
the workflow lives on. Scheduled workflows run from the **default branch**,
so whatever branch is set as default in Settings → Branches is the one that
publishes daily. That's fine as-is; if you later rename/replace the default
branch, keep `.github/workflows/update.yml` on it.

## Running locally

```sh
pip install requests beautifulsoup4
export WFB_EMAIL='you@example.com'
export WFB_PASSWORD='...'
python scraper.py            # writes ./bin-clean.ics
# WFB_DEBUG=1 python scraper.py   # also prints the schedule table structure
```

## How it works / maintenance notes

- `scraper.py` logs in by replicating the portal's own login form
  submission: it fetches the `_Login` partial (which carries an
  anti-forgery token and an anti-bot honeypot with a per-session key),
  fills in the credentials, and POSTs the whole form to `/Token` for a
  bearer token. Then it reads the account page for the booking id and
  fetches `/home/schedule`.
- The `/home/schedule` fragment is a grid of `<div>`s (not a table). Each
  week row has a `weekcell` (the week-commencing Monday label — not a
  clean), `No Clean` cells, and `bincell` cells that are the actual cleans
  (e.g. `bincell blackbin`). The scraper reads the `bincell` cells and
  de-duplicates, so you get exactly the real cleans across all bins
  (regular black-bin plus any ad-hoc blue/green ones).
- Events use stable UIDs (`wfb-<hash>-<date>@binclean`, where `<hash>` is a
  one-way hash of the booking id — the raw number never appears in the
  published file), so re-runs update rather than duplicate.
- The scraper refuses to write the file on missing/implausible dates, and
  the workflow refuses to deploy an empty file — a portal change fails the
  run loudly instead of publishing a broken calendar.
