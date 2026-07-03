# Wheelie Fresh Bins → calendar feed

A daily GitHub Actions job logs into the [Wheelie Fresh Bins customer
portal](https://portal.wheeliefreshbins.com/), reads the bin cleaning
schedule, and commits it to this repo as `bin-clean.ics`. Calendar apps
subscribe to the raw file URL and stay up to date automatically.

No address or personal details are written into the calendar file — just
all-day "Bin clean" events with a reminder the evening before. Bin
Holidays recorded on the portal are excluded automatically.

## One-time setup (all doable from a phone)

### 1. Add the portal credentials as secrets

1. Open this repo on github.com → **Settings** (on mobile web, tap the
   **⋯** / kebab menu if Settings isn't visible) → **Secrets and
   variables** → **Actions**.
2. Tap **New repository secret** and add:
   - Name `WFB_EMAIL`, value: the email you log into the portal with.
   - Name `WFB_PASSWORD`, value: your portal password.

### 2. Enable and test the workflow

1. Go to the **Actions** tab. If prompted, tap **I understand my
   workflows, enable them**.
2. Open the **Update bin-clean.ics** workflow → **Run workflow** to do a
   first manual run.
3. When it goes green, `bin-clean.ics` appears in the repo. If it goes
   red, open the run log — the scraper prints why (bad credentials,
   portal layout change, etc.) and GitHub emails you about the failure.

It then runs by itself every day at about 06:00 UTC.

### 3. Get the raw URL

Open `bin-clean.ics` in the repo and copy the **Raw** link. It looks like:

```
https://raw.githubusercontent.com/<user>/<repo>/main/bin-clean.ics
```

(Swap `main` for the default branch if yours differs. If the repo is
private, raw URLs need auth and calendar apps can't subscribe — make the
repo public, or use a private gist instead. The ICS contains only dates,
no address.)

### 4. Subscribe in your calendar

**Google Calendar** (must be done on the web, not the app):

1. Open [calendar.google.com](https://calendar.google.com) in a browser —
   on a phone, request the desktop site if needed.
2. **Settings** (gear) → **Add calendar** → **From URL**.
3. Paste the raw URL → **Add calendar**.
4. It appears under "Other calendars" and syncs to the phone app. Google
   refreshes subscribed calendars on its own schedule (every several
   hours up to ~a day).

**Apple Calendar (iPhone/iPad):**

1. **Settings** → **Apps** → **Calendar** → **Calendar Accounts** →
   **Add Account** → **Other** → **Add Subscribed Calendar**.
2. Paste the raw URL and tap **Next** → **Save**.

## Running locally

```sh
pip install requests beautifulsoup4
export WFB_EMAIL='you@example.com'
export WFB_PASSWORD='...'
python scraper.py
```

Writes `bin-clean.ics` in the current directory.

## How it works / maintenance notes

- `scraper.py` logs in once per run (form → `POST /Token` → bearer
  token), reads the account page for the booking id, bookings table and
  holiday table, then asks the portal's schedule endpoint for upcoming
  clean dates. If that endpoint can't be read it falls back to the
  "Next Clean" column on the account page.
- Events get stable UIDs (`wfb-<bookingId>-<date>@binclean`), so re-runs
  update rather than duplicate, and the file is only committed when the
  schedule actually changes.
- The scraper refuses to write the file if it finds no dates or
  implausible dates, so a portal change breaks the workflow loudly
  instead of silently emptying your calendar.
