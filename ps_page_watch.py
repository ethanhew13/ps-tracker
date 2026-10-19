#!/usr/bin/env python3
"""
Watches specific web pages and pings Discord when they change.
Add or remove pages in WATCH / KEYWORD_WATCH below. Stdlib only, no installs.
Needs ps_common.py in the same folder.

Usage:  python ps_page_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

Two kinds of watching:
  WATCH          full-page: meta tags, every image URL and the visible text. Any change alerts.
  KEYWORD_WATCH  only GTA-related stuff (see KEYWORDS): links, text lines and image URLs that
                 mention it. Alerts when those appear or disappear, ignores everything else.

Every alert is a readable summary plus two attached HTML files (before and after) with the
differences highlighted. Pages that return 404 (not live yet) are remembered and you get a
"now LIVE" alert the moment they appear. First run just saves a baseline.
"""
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin

from ps_common import (UA, change_alert, gone_alert, image_urls, live_alert, parse_meta,
                       send_alerts, to_lines)

WATCH = {
    "ps-gta6-page": "https://www.playstation.com/en-us/games/grand-theft-auto-vi/",
    "store-gta6-standard": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVISTANDARD001",
    "store-gta6-ultimate": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVIULTIMATE001",
    "support-gta6-offer-terms": "https://www.playstation.com/en-us/support/games/gta-vi-offer-terms",
    "support-gta6-game": "https://www.playstation.com/en-us/support/games/gta-vi-game/",
}

KEYWORD_WATCH = {
    "sitemap": "https://www.playstation.com/en-us/site-map/",
    "support-games-list": "https://www.playstation.com/en-us/support/games/?category=game-support&subCategory=support-for-specific-games",
}

# What counts as "GTA stuff" on keyword pages. Edit freely.
KEYWORDS = re.compile(
    r"\bgta\b|gta[\s_-]*(?:vi|6)\b|gtavi|grand[\s_-]*theft[\s_-]*auto|vice[\s_-]*city|leonida",
    re.I)

STATE_FILE = Path(__file__).with_name("ps_page_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone

CONFIRM = False       # True = require the change on 2 consecutive runs before alerting
SNAP_V = 2            # bump when the snapshot format changes (triggers a silent re-baseline)
MAX_STATE = 150_000   # max chars of page text kept in the state file

# meta tags that change on every deploy and aren't real content changes
IGNORE_META = {"wca-ssr-version", "wca-theme", "viewport", "next-head-count",
               "content-page-ref", "csrf-token"}
# visible-text lines to ignore (regex). Add noisy stuff here if you get false alerts.
IGNORE_LINES = [re.compile(p) for p in [r"^©"]]

A_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*(?:"([^"]*)"|'([^']*)')[^>]*>(.*?)</a>""", re.I | re.S)
KEYWORD_NOTE = "Keyword watch: only GTA-related links, text and images on this page are shown."


def keyword_lines(page, url):
    """Only the GTA-related links, text lines and image URLs on a page."""
    found = set()
    for m in A_RE.finditer(page):
        href = html.unescape(m.group(1) or m.group(2) or "").strip()
        text = re.sub(r"<[^>]+>", " ", m.group(3))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if KEYWORDS.search(text) or KEYWORDS.search(href):
            found.add(f"[link] {text or '(no text)'} -> {urljoin(url, href)}")
    for line in to_lines(page, IGNORE_LINES):
        if KEYWORDS.search(line):
            found.add(f"[text] {line}")
    for u in image_urls(page):
        if KEYWORDS.search(u):
            found.add(f"[img] {u}")
    return sorted(found)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return "ok", r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"http {e.code}", ""
    except Exception as e:  # network hiccup: skip, don't treat as a change
        return f"error: {e}", ""


def snapshot(url):
    status, body = fetch(url)
    if status != "ok":
        return {"status": status}
    meta = parse_meta(body)
    lines = [f"[meta] {k}: {v}" for k, v in sorted(meta.items()) if k not in IGNORE_META]
    lines += [f"[img] {u}" for u in image_urls(body)]
    lines += to_lines(body, IGNORE_LINES)
    text = "\n".join(lines)
    if len(lines) < 5:  # basically empty page (blocked / JS-only), don't trust it
        return {"status": "empty"}
    return {"status": "ok", "v": SNAP_V, "hash": hashlib.sha1(text.encode()).hexdigest(),
            "text": text[:MAX_STATE]}


def keyword_snapshot(url):
    status, body = fetch(url)
    if status != "ok":
        return {"status": status}
    if len(to_lines(body, IGNORE_LINES)) < 15:  # blocked / JS-only shell, don't trust it
        return {"status": "empty"}
    text = "\n".join(keyword_lines(body, url))
    return {"status": "ok", "v": SNAP_V, "mode": "keywords",
            "hash": hashlib.sha1(text.encode()).hexdigest(), "text": text[:MAX_STATE]}


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def targets():
    for name, url in WATCH.items():
        yield name, url, snapshot, ""
    for name, url in KEYWORD_WATCH.items():
        yield name, url, keyword_snapshot, KEYWORD_NOTE


def main():
    state = load_state()
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M") + " AWST"
    alerts, baselined, notes, pending = [], [], [], []
    for name, url, snap, note in targets():
        new = snap(url)
        status = new["status"]
        old = state.get(name)

        if status in ("http 404", "http 410"):  # page doesn't exist (yet)
            if old and old.get("status") == "ok":
                old["miss"] = old.get("miss", 0) + 1  # needs 2 misses in a row, avoids blips
                if old["miss"] >= 2:
                    alerts.append(gone_alert(name, url, stamp, old.get("text", ""), status[5:], note))
                    state[name] = {"status": "missing", "text": old.get("text", "")}
            elif not old:
                state[name] = {"status": "missing"}
                notes.append(f"{name}: not live yet (404), will alert when it appears")
            continue
        if status != "ok":
            print(f"[{name}] {status} (skipped)", file=sys.stderr)
            continue

        if old and old.get("status") == "missing":  # it just appeared
            if old.get("text"):
                alerts.append(change_alert(name, url, stamp, old["text"], new["text"], "is back", note))
            else:
                alerts.append(live_alert(name, url, stamp, new["text"], note))
            state[name] = new
            continue
        if old:
            old.pop("miss", None)
        if not old or old.get("status") != "ok" or old.get("v") != SNAP_V:
            state[name] = new
            n = len(new["text"].splitlines()) if new.get("mode") == "keywords" else None
            baselined.append(name if n is None else f"{name} ({n} GTA matches)")
            continue
        if new["hash"] == old["hash"]:
            old.pop("pending", None)  # change flipped back, ignore it
            continue
        if CONFIRM and old.get("pending") != new["hash"]:
            old["pending"] = new["hash"]  # first sighting, wait for the next run
            pending.append(name)
            continue
        alerts.append(change_alert(name, url, stamp, old["text"], new["text"], "changed", note))
        state[name] = new

    STATE_FILE.write_text(json.dumps(state, indent=2))
    if alerts:
        send_alerts(alerts, PING_TEXT)
        return
    msgs = []
    if baselined:
        msgs.append(f"baseline saved for: {', '.join(baselined)}")
    msgs += notes
    if pending:
        msgs.append(f"unconfirmed change on: {', '.join(pending)}")
    print(f"[{stamp}] " + ("; ".join(msgs) if msgs else "no changes"))


if __name__ == "__main__":
    main()
