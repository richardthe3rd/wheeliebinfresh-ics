#!/usr/bin/env python3
"""Scrape the Wheelie Fresh Bins customer portal and write bin-clean.ics.

Reads credentials from WFB_EMAIL / WFB_PASSWORD environment variables.
Exits non-zero (without touching an existing bin-clean.ics) on any
login/parse failure so a scheduled run fails loudly rather than
publishing bad data.
"""

import json
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


# --------------------------------------------------------------------------
# Portal client
# --------------------------------------------------------------------------

def _pick_form_html(payload, raw_text):
    """Given the parsed JSON payload (or None) and the raw response text,
    return the string most likely to contain the login form markup."""
    if isinstance(payload, dict):
        # The form markup may sit directly under a key or one level down
        # (e.g. {"data": {"html": "..."}}). Search a couple of common shapes.
        for key in ("html", "Html", "form", "Form", "view", "View",
                    "partial", "content", "body"):
            val = payload.get(key)
            if isinstance(val, str) and "<" in val:
                return val
        for container in ("data", "Data", "result", "Result", "model", "Model"):
            inner = payload.get(container)
            if isinstance(inner, dict):
                for key in ("html", "Html", "form", "Form", "view", "View"):
                    val = inner.get(key)
                    if isinstance(val, str) and "<" in val:
                        return val
            elif isinstance(inner, str) and "<" in inner:
                return inner
    return raw_text


def get_login_form(session):
    """GET /Account/Ajax_Login and return (form_html, antiforgery_token).

    First hits the site root so any anti-forgery / session cookie the login
    partial depends on is present."""
    # Prime cookies: some ASP.NET anti-forgery setups only render the login
    # form's hidden token once a session cookie exists.
    try:
        session.get(f"{BASE_URL}/", timeout=30)
    except requests.RequestException as e:
        print(f"warning: priming GET / failed: {e}")

    r = session.get(f"{BASE_URL}/Account/Ajax_Login",
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Accept": "application/json, text/html"},
                    timeout=30)
    r.raise_for_status()

    payload = None
    try:
        payload = r.json()
    except ValueError:
        pass  # server returned the form as plain HTML

    html = _pick_form_html(payload, r.text)

    soup = BeautifulSoup(html, "html.parser")
    n_inputs = len(soup.find_all("input"))
    if n_inputs == 0:
        # Diagnostics (no credentials are sent on this GET; the login form
        # is public). Print the response shape so a re-run reveals the real
        # structure, then let discovery fail loudly.
        ctype = r.headers.get("Content-Type", "?")
        shape = (f"keys={sorted(payload.keys())}"
                 if isinstance(payload, dict) else f"type={type(payload).__name__}")
        print(f"login form GET: HTTP {r.status_code}, content-type {ctype}, "
              f"{len(r.text)} bytes, json {shape}")
        print("response preview: "
              + redact(r.text[:800], []).replace("\n", " "))

    token_input = soup.find("input", attrs={"name": "__RequestVerificationToken"})
    token = token_input.get("value") if token_input else None
    if not token:
        # The token may also arrive as a cookie or a JSON field.
        for k, v in session.cookies.items():
            if "RequestVerification" in k or "AntiForgery" in k:
                token = v
                break
    if not token and isinstance(payload, dict):
        for key in ("token", "Token", "antiForgeryToken", "__RequestVerificationToken"):
            if isinstance(payload.get(key), str):
                token = payload[key]
                break
    return html, token


# Input types that can never be the username box.
_NON_USER_TYPES = {
    "password", "hidden", "checkbox", "radio", "submit",
    "button", "image", "reset", "file",
}


def discover_credential_fields(form_html):
    """Read the login form markup to find the username and password field
    names rather than assuming them. Returns (user_field, password_field,
    fields) where fields is a list of (name, type) for diagnostics."""
    soup = BeautifulSoup(form_html, "html.parser")
    inputs = soup.find_all("input")

    fields = []
    for inp in inputs:
        name = inp.get("name")
        if name:
            fields.append((name, (inp.get("type") or "text").lower()))

    password_field = None
    for name, itype in fields:
        if itype == "password":
            password_field = name
            break

    # Username: prefer an input whose name/type hints at it; otherwise take
    # the first input that isn't the token, the password, or a control type.
    # This deliberately accepts text, email, tel, search, etc. rather than
    # whitelisting a couple of types, so an unexpected input type on the
    # username box doesn't break login.
    candidates = [
        name for name, itype in fields
        if name != "__RequestVerificationToken"
        and name != password_field
        and itype not in _NON_USER_TYPES
    ]
    user_field = None
    for name in candidates:
        low = name.lower()
        if any(h in low for h in ("user", "email", "login", "name")):
            user_field = name
            break
    if not user_field and candidates:
        user_field = candidates[0]

    # Last-ditch: match on name text even for control-typed inputs.
    if not password_field:
        for name, _ in fields:
            if "password" in name.lower() or "pass" == name.lower():
                password_field = name
                break

    return user_field, password_field, fields


def build_token_body(email, password, form_html, token):
    """Build the POST /Token form body.

    The portal is a JS SPA: unauthenticated routes return the marketing
    homepage, so the login form is never server-rendered for us to read
    field names from. The /Token endpoint is an OWIN OAuth token endpoint,
    which reads the spec-standard lowercase keys `username` and `password`
    from the form body (via context.UserName / context.Password) regardless
    of what the on-screen form labels its boxes. We send those, plus a few
    harmless aliases in case this particular endpoint expects MVC-style
    names. Extra params are ignored by a conformant token endpoint, so this
    stays a single login attempt rather than a probing loop."""
    body = {
        "grant_type": "password",
        "username": email,
        "password": password,
    }

    # If the form actually was readable (it isn't, currently), honour its
    # real field names too.
    user_field, password_field, _ = discover_credential_fields(form_html)
    if user_field:
        body[user_field] = email
    if password_field:
        body[password_field] = password

    # Common ASP.NET MVC aliases, in case the endpoint is custom.
    for alias in ("UserName", "Email"):
        body.setdefault(alias, email)
    body.setdefault("Password", password)

    if token:
        body["__RequestVerificationToken"] = token
    return body


def login(session, email, password):
    """Log in via the OAuth /Token endpoint and attach the bearer token.

    Best-effort: collect cookies and any anti-forgery token first (harmless
    if absent), then make a single POST /Token."""
    form_html, token = get_login_form(session)

    body = build_token_body(email, password, form_html, token)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    if token:
        headers["__RequestVerificationToken"] = token
    print(f"POST /Token (anti-forgery token: {'present' if token else 'none'})")

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
    if token:
        session.headers["__RequestVerificationToken"] = token
    print("login OK")


def get_account_page(session):
    r = session.get(f"{BASE_URL}/", timeout=30)
    r.raise_for_status()
    return r.text


def parse_booking_id(page_html):
    m = re.search(r"var\s+bookingId\s*=\s*(\d+)", page_html)
    return int(m.group(1)) if m else None


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


def fetch_schedule(session, booking_id, today):
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

        dates = extract_dates(body, today)
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
            f"UID:wfb-{booking_id}-{ymd}@binclean",
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
    print(f"bookingId: {booking_id}")

    dates = fetch_schedule(session, booking_id, today)
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
