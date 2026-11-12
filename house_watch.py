#!/usr/bin/env python3
"""
House Watch: get a Discord ping the moment one specific house hits the market.

Checks three places for the address:
  * Zillow          (the house's own page)
  * Realtor.com     (every for-sale and pending listing in the house's ZIP code)
  * the local MLS   (every active, pending and under-contract listing in the ZIP)

Schedule (Central time): every 15 minutes, and once an hour from 12am to 5am.
After the house is listed it also reports price changes, pending, sold and delisted.

The address and the Discord webhook are never stored in this file. They come from
environment variables (GitHub Actions secrets when it runs in the cloud):

  HOUSE_ADDRESS        e.g.  "742 Evergreen Ter, Springfield, MO 65801"
  DISCORD_WEBHOOK_URL  the webhook of the channel that should get the alerts

Install once:   pip install -r requirements.txt
Run one check:  python house_watch.py --once     (what GitHub Actions runs)
Run forever:    python house_watch.py
Test alerts:    python house_watch.py --test
"""

import datetime as dt
import html as htmllib
import json
import os
import re
import sys
import time
import traceback
import urllib.request
from zoneinfo import ZoneInfo

# ─────────────────────────── SETTINGS ───────────────────────────
ADDRESS = os.environ.get("HOUSE_ADDRESS", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DISCORD_MENTION = os.environ.get("DISCORD_MENTION", "@everyone")   # "" for no ping

# Local MLS search (Ste. Genevieve County, MO area). {zip} is filled in from the address.
MLS_SEARCH_URL = os.environ.get(
    "MLS_SEARCH_URL",
    "https://properties.ritterrealestate.com/idx/results/listings"
    "?idxID=c057&ccz=zipcode&zipcode[]={zip}&per=250")

TIMEZONE = "America/Chicago"
DAY_INTERVAL_MIN = 15             # 5am to midnight
NIGHT_INTERVAL_MIN = 60           # midnight to 5am
NIGHT_START_HOUR, NIGHT_END_HOUR = 0, 5
FAILS_BEFORE_WARNING = 8          # failed checks in a row before it tells you

STATE_FILE = os.environ.get("STATE_FILE", os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".state", "house_watch_state.json"))
# ────────────────────────────────────────────────────────────────

STREET_SUFFIXES = {"dr", "drive", "st", "street", "rd", "road", "ave", "avenue", "ln", "lane",
                   "ct", "court", "blvd", "boulevard", "way", "pl", "place", "ter", "terrace",
                   "cir", "circle", "hwy", "highway", "pkwy", "parkway", "trl", "trail"}
SAINT_WORDS = {"saint", "st", "st.", "ste", "ste."}
SEP = r"[\s\-]+"


def parse_address(address):
    """Split the address and build a pattern that matches the ways sites write the street."""
    street = address.split(",")[0].strip()
    m = re.search(r"\b(\d{5})(?:-\d{4})?\s*$", address)
    if not street or not m or not re.match(r"\d+\s+\S", street):
        raise SystemExit('HOUSE_ADDRESS must look like "123 Main St, Town, ST 12345".')
    words = street.replace(".", "").split()
    number, name = words[0], words[1:]
    suffix = None
    if len(name) > 1 and name[-1].lower() in STREET_SUFFIXES:
        suffix = name.pop()
    parts = [r"(?:Saint|St\.?|Ste\.?)" if w.lower() in SAINT_WORDS else re.escape(w) for w in name]
    street_re = r"(?<![\w\-])" + re.escape(number) + SEP + SEP.join(parts) + r"\b"
    redact_re = street_re + (r"(?:" + SEP + r"[A-Za-z]{2,9}\b\.?)?" if suffix else "")
    return street, m.group(1), street_re, redact_re


if ADDRESS:
    STREET, ZIP, STREET_REGEX, _REDACT_REGEX = parse_address(ADDRESS)
    ZILLOW_URL = os.environ.get("ZILLOW_URL") or (
        "https://www.zillow.com/homes/" + re.sub(r"[^A-Za-z0-9]+", "-", ADDRESS).strip("-") + "_rb/")
else:
    STREET = ZIP = STREET_REGEX = _REDACT_REGEX = ZILLOW_URL = None

REALTOR_URL = f"https://www.realtor.com/realestateandhomes-search/{ZIP}" if ZIP else None
MLS_URL = MLS_SEARCH_URL.format(zip=ZIP) if ZIP else None


def local_now():
    return dt.datetime.now(ZoneInfo(TIMEZONE))


def redact(text):
    """Keep the address out of logs (GitHub Actions logs are public on a public repo)."""
    text = str(text)
    if not ADDRESS:
        return text
    text = text.replace(ADDRESS, "[the house]")
    text = re.sub(_REDACT_REGEX, "[the house]", text, flags=re.I)
    return re.sub(r"https?://\S+", "[link]", text)


def log(msg):
    print(f"[{local_now():%Y-%m-%d %I:%M:%S %p}] {redact(msg)}", flush=True)


# ─────────────────────────── ALERTS ───────────────────────────
def notify(title, body, url=None, urgent=False):
    log(f"ALERT: {title} | {body}")
    if not DISCORD_WEBHOOK_URL:
        log("  (no DISCORD_WEBHOOK_URL set, so nothing was sent)")
        return False
    content = (f"{DISCORD_MENTION} " if urgent and DISCORD_MENTION else "") + f"**{title}**\n{body}"
    if url:
        content += f"\n{url}"
    payload = {"content": content[:1990], "allowed_mentions": {"parse": ["everyone"] if urgent else []}}
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "house-watch-bot"})
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log(f"  (Discord alert failed: {type(e).__name__}: {getattr(e, 'code', '')})")
        return False


# ─────────────────────────── CHECKERS ───────────────────────────
# Each returns a dict; "listed" says whether that site shows the house as on the market.
_session = None


def http_get(url):
    global _session
    from curl_cffi import requests as cffi
    if _session is None:
        _session = cffi.Session(impersonate="chrome")
    r = _session.get(url, timeout=30)
    if r.status_code in (403, 429) or "px-captcha" in r.text or "Press & Hold" in r.text:
        _session = None
        raise RuntimeError(f"blocked (HTTP {r.status_code})")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r


def check_zillow():
    """Reads the house's own Zillow page."""
    page = http_get(ZILLOW_URL).text
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    if not m:
        raise RuntimeError("page format not recognized")
    try:
        cache = json.loads(m.group(1))["props"]["pageProps"]["componentProps"]["gdpClientCache"]
        cache = json.loads(cache) if isinstance(cache, str) else cache
        prop = next(v["property"] for v in cache.values() if isinstance(v, dict) and v.get("property"))
    except (KeyError, StopIteration, TypeError):
        raise RuntimeError("Zillow did not open the house's page")
    if not re.search(STREET_REGEX, prop.get("streetAddress") or "", re.I):
        raise RuntimeError("Zillow opened a different property")
    status = prop.get("homeStatus")
    on_market = status not in (None, "OTHER", "SOLD", "RECENTLY_SOLD", "OFF_MARKET")
    hist = prop.get("priceHistory") or []
    latest = hist[0] if hist else {}
    return {
        "status": status,
        "status_detail": prop.get("keystoneHomeStatus"),
        # when unlisted, Zillow's "price" is only the Zestimate, so ignore it
        "list_price": (prop.get("listPrice") or prop.get("price")) if on_market else None,
        "date_posted": prop.get("datePostedString"),
        "latest_event": f'{latest.get("date")} {latest.get("event")} {latest.get("price")}' if latest else None,
        "listed": on_market,
    }


def check_realtor():
    """Searches every for-sale and pending Realtor.com listing in the ZIP for the address."""
    from homeharvest import scrape_property
    rows = []
    for listing_type in ("for_sale", "pending"):      # one call each; a combined call drops results
        rows += scrape_property(location=ZIP, listing_type=[listing_type], return_type="raw") or []
    if not rows:
        raise RuntimeError("no listings came back for the ZIP code")
    for row in rows:
        line = ((row.get("location") or {}).get("address") or {}).get("line") or ""
        if not re.search(STREET_REGEX, f"{line} | {row.get('permalink') or ''}", re.I):
            continue
        flags = row.get("flags") or {}
        return {
            "status": row.get("status"),
            "mls_status": row.get("mls_status"),
            "list_price": row.get("list_price"),
            "list_date": row.get("list_date"),
            "pending": bool(flags.get("is_pending") or flags.get("is_contingent") or row.get("pending_date")),
            "listed": True,
            "_url": row.get("href"),
        }
    return {"status": None, "mls_status": None, "list_price": None, "list_date": None,
            "pending": False, "listed": False}


def check_local_mls():
    """Scans the local MLS search (all agencies) for the address."""
    page = http_get(MLS_URL).text
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", text)))
    if "Listing ID" not in text and not re.search(r"\b0 results returned", text):
        raise RuntimeError("no listings parsed (page format may have changed)")
    m = re.search(STREET_REGEX, text, re.I)
    if not m:
        return {"status": None, "list_price": None, "mls_number": None, "listed": False}
    card = text[m.start():m.start() + 400]
    price = re.search(r"Price\s+(\$[\d,]+)", card)
    status = re.search(r"Status\s+(.+?)\s+(?:Bed|Total|Full|Half|Bath|Acre|Lot|SqFt|Sub)", card)
    mls = re.search(r"Listing ID\s+(\w+)", card)
    return {
        "status": status.group(1) if status else "listed",
        "list_price": price.group(1) if price else None,
        "mls_number": mls.group(1) if mls else None,
        "listed": True,
    }


SITES = {
    "Zillow": (check_zillow, ZILLOW_URL),
    "Realtor.com": (check_realtor, REALTOR_URL),
    "Local MLS": (check_local_mls, MLS_URL),
}


# ─────────────────────────── LOGIC ───────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def describe(snap):
    return ", ".join(f"{k}: {v}" for k, v in snap.items()
                     if v not in (None, "", "None", False) and k != "listed")


def run_checks(state):
    first_run = not state.get("snapshots")
    snaps = state.setdefault("snapshots", {})
    fails = state.setdefault("fails", {})
    summary = []
    for name, (fn, url) in SITES.items():
        try:
            raw = fn()
            url = raw.pop("_url", None) or url
            snap = {k: (v if isinstance(v, bool) else (str(v) if v is not None else None))
                    for k, v in raw.items()}
        except Exception as e:
            fails[name] = fails.get(name, 0) + 1
            log(f"{name}: check failed ({fails[name]} in a row): {e}")
            summary.append(f"{name}: could not check")
            if fails[name] == FAILS_BEFORE_WARNING:
                notify(f"House Watch: {name} checks failing",
                       f"{name} has failed {FAILS_BEFORE_WARNING} checks in a row. "
                       "The other sites are still being checked.")
            continue
        if fails.get(name, 0) >= FAILS_BEFORE_WARNING:
            notify(f"House Watch: {name} working again", f"{name} checks recovered.")
        fails[name] = 0
        summary.append(f"{name}: {'LISTED' if snap['listed'] else 'not listed'}")

        old = snaps.get(name)
        if old is None:
            log(f"{name}: first check, {'LISTED' if snap['listed'] else 'not listed'} ({describe(snap) or 'no data'})")
            if snap["listed"]:
                notify(f"🚨 {STREET} is LISTED on {name}!", describe(snap), url, urgent=True)
        elif snap["listed"] and not old.get("listed"):
            notify(f"🚨 {STREET} just got LISTED on {name}!", describe(snap), url, urgent=True)
        elif old.get("listed") and not snap["listed"]:
            notify(f"{STREET} no longer shows as listed on {name}", describe(snap) or "off market", url)
        elif snap["listed"] and snap != old:
            changes = [f"{k}: {old.get(k)} → {v}" for k, v in snap.items() if old.get(k) != v]
            notify(f"{STREET} listing changed on {name}", "\n".join(changes), url, urgent=True)
        else:
            log(f"{name}: no change ({'LISTED' if snap['listed'] else 'not listed'})")
        snaps[name] = snap

    if first_run and snaps:
        notify("House Watch is live 🏠",
               f"Watching {ADDRESS}. You'll get a ping here the moment it shows as listed.\n"
               + "\n".join(summary))
    state["last_check"] = local_now().isoformat()
    save_state(state)


def interval_minutes(t):
    return NIGHT_INTERVAL_MIN if NIGHT_START_HOUR <= t.hour < NIGHT_END_HOUR else DAY_INTERVAL_MIN


def next_run_after(t):
    """Next slot on the clock: :00/:15/:30/:45 in the day, top of the hour at night."""
    t = t.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    while t.minute % interval_minutes(t) != 0:
        t += dt.timedelta(minutes=1)
    return t


def due_now(state):
    """For --once mode (scheduler fires every 15 min): skip the extra night-time runs."""
    t = local_now()
    if interval_minutes(t) == DAY_INTERVAL_MIN:
        return True
    last = state.get("last_check")
    if not last:
        return True
    return (t - dt.datetime.fromisoformat(last)) >= dt.timedelta(minutes=NIGHT_INTERVAL_MIN - 8)


def main():
    if not ADDRESS or not DISCORD_WEBHOOK_URL:
        missing = " and ".join(n for n, v in (("HOUSE_ADDRESS", ADDRESS),
                                              ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)) if not v)
        msg = f"House Watch is not set up yet: add the {missing} secret(s). See the README."
        print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else msg, flush=True)
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        if not ADDRESS:
            return

    if "--test" in sys.argv:
        ok = notify("House Watch test 🏠", f"Alerts are working. Watching {ADDRESS}.")
        sys.exit(0 if ok else 1)

    state = load_state()
    if "--once" in sys.argv:
        if due_now(state):
            run_checks(state)
        else:
            log("Night hours: already checked this hour, skipping.")
        return

    log(f"Watching [the house] on {', '.join(SITES)}")
    log(f"Every {DAY_INTERVAL_MIN} min, hourly from 12am to 5am ({TIMEZONE}). Ctrl+C to stop.")
    while True:
        run_checks(state)
        nxt = next_run_after(local_now())
        log(f"Next check at {nxt:%I:%M %p}")
        while local_now() < nxt:
            time.sleep(min(30, max(1, (nxt - local_now()).total_seconds())))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
    except Exception:
        print(redact(traceback.format_exc()), file=sys.stderr, flush=True)
        notify("House Watch crashed", "The check hit an unexpected error. See the latest run on GitHub.")
        sys.exit(1)
