#!/usr/bin/env python3
"""Scrape the Wheelie Fresh Bins customer portal and write bin-clean.ics.

Reads credentials from WFB_EMAIL / WFB_PASSWORD environment variables.
Exits non-zero (without touching an existing bin-clean.ics) on any
login/parse failure so a scheduled run fails loudly rather than
publishing bad data.
"""

import hashlib
import os
import re
import sys
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://portal.wheeliefreshbins.com"
ICS_PATH = "bin-clean.ics"
SUMMARY = "Bin clean (Wheelie Fresh Bins)"

# Sanity window for parsed clean dates: anything outside this range means
# parsing has gone wrong and we must not ship the result.
PAST_SLACK = timedelta(days=45)
FUTURE_SLACK = timedelta(days=400)

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def redact(text, secrets):
    """Strip credential values and anything token-shaped from text before printing."""
    if not text:
        return text
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    # OAuth/JWT-ish blobs and *_token JSON fields
    text = re.sub(r'"(access|refresh)_token"\s*:\s*"[^"]*"',
                  r'"\1_token": "[REDACTED]"', text)
    text = re.sub(r"[A-Za-z0-9_\-\.]{80,}", "[REDACTED]", text)
    return text


# --------------------------------------------------------------------------
# Date parsing
# --------------------------------------------------------------------------

def infer_year(day, month, today):
    """Assign a year to a day/month with no year: current year unless the
    date passed more than ~2 weeks ago, in which case next year."""
    candidate = date(today.year, month, day)
    if candidate < today - timedelta(days=14):
        candidate = date(today.year + 1, month, day)
    return candidate


def parse_date_str(text, today):
    """Parse one date from a short string. Handles '03 Jul', '03 Jul 2026',
    '03 July 2026', 'Friday 03 July 2026', '03/07/2026' (UK order),
    and ISO '2026-07-03'. Returns a date or None."""
    text = text.strip()
    if not text:
        return None

    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d)
        except ValueError:
            return None

    # '03 Jul', '3rd July 2026', 'Fri 03 Jul' etc.
    m = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
        r"(?:\s+(\d{4}))?\b",
        text, re.IGNORECASE)
    if m:
        d = int(m.group(1))
        mo = MONTHS[m.group(2).lower()[:3]]
        try:
            if m.group(3):
                return date(int(m.group(3)), mo, d)
            return infer_year(d, mo, today)
        except ValueError:
            return None

    return None


def extract_dates(html, today):
    """Pull every recognisable date out of an HTML fragment (the schedule
    modal body). Looks at table cells and list items first, then falls back
    to scanning all text."""
    soup = BeautifulSoup(html, "html.parser")
    found = []

    cells = soup.find_all(["td", "li"])
    sources = [c.get_text(" ", strip=True) for c in cells]
    if not sources:
        sources = [soup.get_text("\n", strip=True)]

    for chunk in sources:
        for line in chunk.split("\n"):
            d = parse_date_str(line, today)
            if d:
                found.append(d)

    seen = set()
    unique = []
    for d in found:
        if d not in seen:
            seen.add(d)
            unique.append(d)
    return sorted(unique)


def parse_schedule_dates(html, today):
    """Extract the booking's clean dates from the /home/schedule fragment.

    The fragment is a table: the first column is "W/C" (week commencing) —
    a Monday label for each week, NOT a clean date — and the remaining
    "Bin Cleans" columns hold the actual clean dates (or "No Clean"). We must
    skip the W/C column, otherwise every weekly Monday is wrongly treated as
    a clean (that produced 16 phantom Monday events alongside the 8 real
    Friday cleans). Verified against the portal's printable schedule."""
    soup = BeautifulSoup(html, "html.parser")

    target = None
    wc_col = 0
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(" ", strip=True).lower()
                  for c in rows[0].find_all(["th", "td"])]
        if any("bin clean" in h for h in header) or \
                any(h in ("w/c", "wc", "week commencing") for h in header):
            target = table
            for i, h in enumerate(header):
                if "w/c" in h or h.startswith("week"):
                    wc_col = i
                    break
            break

    if target is None:
        # Unexpected layout: no recognisable schedule table. Rather than grab
        # every date (which over-collects), signal "nothing parsed" so the
        # caller falls back to the Next Clean column.
        return []

    dates = []
    for row in target.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        for i, cell in enumerate(cells):
            if i == wc_col:
                continue  # week-commencing label, not a clean date
            d = parse_date_str(cell.get_text(" ", strip=True), today)
            if d:
                dates.append(d)
    return sorted(set(dates))


# --------------------------------------------------------------------------
# Portal client
# --------------------------------------------------------------------------

def fetch_login_form(session):
    """Fetch the `_Login` partial and return its HTML.

    The portal is a JS SPA. The login form is a server-rendered partial the
    app pulls in via POST /Account/Ajax_Login (a plain GET 302s to a
    NotFound page). This POST also sets the ASP.NET session and anti-forgery
    cookies that the subsequent /Token call depends on, so it must run in
    the same requests.Session."""
    r = session.post(f"{BASE_URL}/Account/Ajax_Login",
                     data={"partial": "_Login", "formId": "Login"},
                     headers={"X-Requested-With": "XMLHttpRequest",
                              "Accept": "application/json"},
                     timeout=30)
    r.raise_for_status()

    html = None
    try:
        payload = r.json()
        if isinstance(payload, dict):
            for key in ("html", "Html", "view", "partial", "data"):
                if isinstance(payload.get(key), str):
                    html = payload[key]
                    break
    except ValueError:
        pass
    if html is None:
        html = r.text
    return html


def parse_login_form(html):
    """Parse the login partial into (fields, user_field, password_field, rvt).

    `fields` maps every input name to the value we should submit, defaults
    preserved verbatim. This is what makes login work: the form carries an
    anti-forgery token plus an anti-bot honeypot (a text input that must be
    submitted empty and an encrypted `__htpKey`); the server validates all
    of them and returns a generic `invalid_grant` if any is missing, even
    with correct credentials. Preserving every field's default value keeps
    the honeypot empty and the key intact; we only overwrite the credential
    fields. `user_field`/`password_field` are located by input type (email /
    password), not by a hard-coded name."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    user_field = password_field = rvt = None

    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        value = inp.get("value") or ""
        classes = " ".join(inp.get("class") or [])

        if itype == "submit" or itype == "button":
            continue
        if name == "__RequestVerificationToken":
            rvt = value
        if itype == "password" and not password_field:
            password_field = name
        elif itype == "email" and not user_field:
            user_field = name

        if itype in ("checkbox", "radio"):
            # Replicate an unchecked box: omit it (matches the SPA default).
            if not inp.has_attr("checked"):
                continue
            value = value or "true"
        # Preserve the field verbatim (honeypot stays empty, keys intact).
        fields[name] = value

    # Fallback if the email input wasn't type=email: first text field that
    # isn't the honeypot (class wfb-htp) or a framework hidden field.
    if not user_field:
        for inp in soup.find_all("input"):
            name = inp.get("name")
            itype = (inp.get("type") or "text").lower()
            classes = " ".join(inp.get("class") or [])
            if (itype in ("text", "email") and name
                    and "wfb-htp" not in classes
                    and not name.startswith("__")):
                user_field = name
                break

    return fields, user_field, password_field, rvt


def login(session, email, password):
    """Log in by replicating the SPA's login-form submission to /Token.

    Fetch the login partial (which sets the session/anti-forgery cookies and
    carries the honeypot fields), fill in the credentials, and POST the whole
    form to /Token with grant_type=password and the anti-forgery header."""
    html = fetch_login_form(session)
    fields, user_field, password_field, rvt = parse_login_form(html)
    if not user_field or not password_field:
        names = ", ".join(sorted(fields)) or "(none)"
        fail("could not locate the username/password inputs in the login "
             f"form (user={user_field!r}, password={password_field!r}). "
             f"Form fields: {names}")

    body = dict(fields)                 # honeypot (empty), __htpKey, RVT, ...
    body[user_field] = email
    body[password_field] = password
    body["grant_type"] = "password"

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    if rvt:
        headers["__RequestVerificationToken"] = rvt
    print(f"POST /Token as {user_field}/{password_field} "
          f"(anti-forgery token: {'present' if rvt else 'MISSING'}, "
          f"{len(fields)} form fields)")

    r = session.post(f"{BASE_URL}/Token", data=body, headers=headers, timeout=30)

    payload = None
    try:
        payload = r.json()
    except ValueError:
        pass

    if r.status_code != 200 or not isinstance(payload, dict) \
            or "access_token" not in payload:
        print(f"login failed: HTTP {r.status_code} "
              f"(content-type {r.headers.get('Content-Type', '?')})",
              file=sys.stderr)
        print(redact(r.text[:2000], [email, password]), file=sys.stderr)
        sys.exit(1)

    session.headers["Authorization"] = f"Bearer {payload['access_token']}"
    if rvt:
        session.headers["__RequestVerificationToken"] = rvt
    print("login OK")


def get_account_page(session):
    r = session.get(f"{BASE_URL}/", timeout=30)
    r.raise_for_status()
    return r.text


def parse_booking_id(page_html):
    m = re.search(r"var\s+bookingId\s*=\s*(\d+)", page_html)
    return int(m.group(1)) if m else None


def uid_slug(booking_id):
    """A stable, non-reversible token for event UIDs. Keeps the raw booking
    id out of the published (public) calendar while staying constant across
    runs so re-runs update events instead of duplicating them."""
    return hashlib.sha256(f"wfb:{booking_id}".encode()).hexdigest()[:16]


def find_table_column(table, header_text):
    """Return the 0-based index of the column whose header contains
    header_text (case-insensitive), or None."""
    header_row = table.find("tr")
    if not header_row:
        return None
    for i, th in enumerate(header_row.find_all(["th", "td"])):
        if header_text.lower() in th.get_text(" ", strip=True).lower():
            return i
    return None


def parse_next_cleans(page_html, today):
    """Fallback source: the 'Next Clean' column of #tblBookings."""
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.find(id="tblBookings")
    if not table:
        return []
    col = find_table_column(table, "Next Clean")
    if col is None:
        return []

    dates = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) > col:
            d = parse_date_str(cells[col].get_text(" ", strip=True), today)
            if d:
                dates.append(d)
    return sorted(set(dates))


def get_address(page_html):
    """Return the account address from #tblBookings (used only to redact it
    from debug output). Best-effort; empty string if not found."""
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.find(id="tblBookings")
    if not table:
        return ""
    col = find_table_column(table, "Address")
    rows = table.find_all("tr")
    if col is None or len(rows) < 2:
        return ""
    cells = rows[1].find_all("td")
    return cells[col].get_text(" ", strip=True) if len(cells) > col else ""


def parse_holidays(page_html, today):
    """Return a list of (first_day, last_day) ranges from #tblHolidays."""
    soup = BeautifulSoup(page_html, "html.parser")
    table = soup.find(id="tblHolidays")
    if not table:
        return []
    first_col = find_table_column(table, "First Day")
    last_col = find_table_column(table, "Last Day")

    ranges = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if first_col is not None and last_col is not None \
                and len(cells) > max(first_col, last_col):
            start = parse_date_str(cells[first_col].get_text(" ", strip=True), today)
            end = parse_date_str(cells[last_col].get_text(" ", strip=True), today)
        else:
            # Header not found: fall back to any two dates in the row
            row_dates = [parse_date_str(c.get_text(" ", strip=True), today)
                         for c in cells]
            row_dates = [d for d in row_dates if d]
            start, end = (row_dates + [None, None])[:2]
        if start and end:
            ranges.append((min(start, end), max(start, end)))
    return ranges


def dump_schedule_debug(label, body, today, secrets):
    """Print the structure of a schedule fragment so its real layout can be
    understood without shipping guessed dates. Redacts the account address
    and email. Only runs when WFB_DEBUG is set (manual dispatch)."""
    print(f"\n----- DEBUG schedule fragment [{label}] ({len(body)} bytes) -----")
    # Raw excerpt (redacted) so the true markup is visible even if it isn't a
    # table (e.g. a list or divs).
    print("raw excerpt: " + redact(body[:2000], secrets).replace("\n", " "))
    soup = BeautifulSoup(body, "html.parser")
    tables = soup.find_all("table")
    print(f"tables: {len(tables)}")
    for ti, table in enumerate(tables):
        tid = table.get("id") or table.get("class") or "(no id)"
        rows = table.find_all("tr")
        header = [c.get_text(" ", strip=True)
                  for c in (rows[0].find_all(["th", "td"]) if rows else [])]
        print(f"  table[{ti}] id={tid} rows={len(rows)} header={header}")
        for r in rows[1:4]:
            cells = [c.get_text(" ", strip=True) for c in r.find_all(["td", "th"])]
            print("    row:", redact(" | ".join(cells), secrets))
    # Each date with its immediate row/li context, to see which column/list
    # the real clean dates live in.
    print("dates in context:")
    for el in soup.find_all(["td", "li"]):
        d = parse_date_str(el.get_text(" ", strip=True), today)
        if d:
            parent = el.find_parent(["tr", "ul", "ol"])
            ctx = parent.get_text(" | ", strip=True) if parent else el.get_text(strip=True)
            print(f"    {d}: {redact(ctx[:160], secrets)}")
    print("----- END DEBUG -----\n")


def fetch_schedule(session, booking_id, today, debug=False, secrets=()):
    """Try the /home/schedule endpoint variants in order and return the
    first list of dates that parses. The exact method/encoding used by the
    site's modal plugin is unverified, hence the small candidate set."""
    candidates = []
    for prefix in ("", "/api"):
        url = f"{BASE_URL}{prefix}/home/schedule"
        candidates.extend([
            ("POST form", lambda u=url: session.post(
                u, data={"bookingId": booking_id},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)),
            ("GET query", lambda u=url: session.get(
                u, params={"bookingId": booking_id},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)),
            ("POST json", lambda u=url: session.post(
                u, json={"bookingId": booking_id},
                headers={"X-Requested-With": "XMLHttpRequest"}, timeout=30)),
        ])

    for label, do_request in candidates:
        try:
            r = do_request()
        except requests.RequestException as e:
            print(f"schedule {label}: request error {e}")
            continue
        if r.status_code != 200:
            print(f"schedule {label}: HTTP {r.status_code}")
            continue

        body = r.text
        try:
            payload = r.json()
            if isinstance(payload, dict):
                if payload.get("expired") or payload.get("isOk") is False:
                    print(f"schedule {label}: API error response")
                    continue
                for key in ("html", "Html", "body", "data"):
                    if isinstance(payload.get(key), str):
                        body = payload[key]
                        break
        except ValueError:
            pass  # HTML fragment, as expected

        if debug:
            dump_schedule_debug(label, body, today, secrets)
        dates = parse_schedule_dates(body, today)
        if dates:
            print(f"schedule {label}: parsed {len(dates)} date(s)")
            return dates
        print(f"schedule {label}: 200 but no dates found")

    return []


# --------------------------------------------------------------------------
# ICS generation
# --------------------------------------------------------------------------

def ics_escape(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def build_ics(dates, booking_id):
    slug = uid_slug(booking_id)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//wheeliebinfresh-ics//bin-clean//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Bin cleans",
    ]
    for d in dates:
        ymd = d.strftime("%Y%m%d")
        next_day = (d + timedelta(days=1)).strftime("%Y%m%d")
        lines.extend([
            "BEGIN:VEVENT",
            f"UID:wfb-{slug}-{ymd}@binclean",
            # Deliberately derived from the event date, not "now", so the
            # file is byte-identical across runs unless the schedule changes.
            f"DTSTAMP:{ymd}T000000Z",
            f"DTSTART;VALUE=DATE:{ymd}",
            f"DTEND;VALUE=DATE:{next_day}",
            f"SUMMARY:{ics_escape(SUMMARY)}",
            "TRANSP:TRANSPARENT",
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{ics_escape(SUMMARY)}",
            "TRIGGER:-PT12H",
            "END:VALARM",
            "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    email = os.environ.get("WFB_EMAIL")
    password = os.environ.get("WFB_PASSWORD")
    if not email or not password:
        fail("WFB_EMAIL and WFB_PASSWORD environment variables must be set")

    today = date.today()
    session = requests.Session()
    session.headers["User-Agent"] = (
        "bin-clean-ics/1.0 (personal calendar feed; one login per day)")

    login(session, email, password)

    page = get_account_page(session)
    booking_id = parse_booking_id(page)
    if not booking_id:
        fail("could not find bookingId on the account page; "
             "page layout may have changed")
    # Log the public UID slug, not the raw id (the id only ever leaves the
    # program as this one-way hash).
    print(f"booking resolved (uid slug {uid_slug(booking_id)})")

    debug = bool(os.environ.get("WFB_DEBUG"))
    # Address is only collected to redact it from any debug output.
    secrets = [email, get_address(page)]
    dates = fetch_schedule(session, booking_id, today, debug=debug, secrets=secrets)
    source = "schedule endpoint"
    if not dates:
        print("schedule endpoint unusable; falling back to Next Clean column")
        dates = parse_next_cleans(page, today)
        source = "Next Clean column"
    if not dates:
        fail("no clean dates found from either the schedule endpoint or "
             "the bookings table")

    bad = [d for d in dates
           if d < today - PAST_SLACK or d > today + FUTURE_SLACK]
    if bad:
        fail(f"parsed implausible dates {bad} (from {source}); "
             "refusing to write the ICS")

    holidays = parse_holidays(page, today)
    if holidays:
        print(f"holiday ranges: {holidays}")
    kept = [d for d in dates
            if not any(start <= d <= end for start, end in holidays)]
    skipped = len(dates) - len(kept)
    if skipped:
        print(f"excluded {skipped} clean(s) falling within bin holidays")
    if not kept:
        fail("all parsed clean dates fall within holiday ranges; "
             "refusing to write an empty calendar")

    # Only future-facing events matter for the feed, but keep the last few
    # weeks so a clean earlier this week doesn't vanish from the calendar.
    kept = [d for d in kept if d >= today - timedelta(days=21)]
    if not kept:
        fail("no current or future clean dates after filtering")

    ics = build_ics(kept, booking_id)
    with open(ICS_PATH, "w", newline="") as f:
        f.write(ics)
    print(f"wrote {ICS_PATH}: {len(kept)} event(s) from {source}: "
          + ", ".join(d.isoformat() for d in kept))


if __name__ == "__main__":
    main()
