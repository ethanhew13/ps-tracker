#!/usr/bin/env python3
"""
Tracks the "This Month on PlayStation" page across regions and tells you when
any of them get updated.

Usage:
  python ps_tmop_tracker.py                 # check once (good for cron / Task Scheduler)
  python ps_tmop_tracker.py --loop 15       # keep checking every 15 minutes
  python ps_tmop_tracker.py --regions en-us en-au   # only check these
  python ps_tmop_tracker.py --list          # show last known state

Optional: set DISCORD_WEBHOOK_URL to get pings in a Discord channel.
No pip installs needed (stdlib only). Needs ps_common.py in the same folder. First run just saves a baseline.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ps_common import send_alerts

URL = "https://www.playstation.com/{region}/editorial/this-month-on-playstation/"
STATE_FILE = Path(__file__).with_name("ps_tmop_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

MISS_LIMIT = 3       # a live page must be missing this many runs in a row before it counts as gone
OUTAGE_SHARE = 0.5   # if more than this share of regions fail in one run, assume an outage and ignore the run

# Edit freely. Regions without the page just show up as "missing" and are skipped.
REGIONS = [
    "en-us", "en-gb", "en-au", "en-ca", "fr-ca", "en-ie", "en-nz", "en-in",
    "en-za", "en-ae", "en-sg", "en-hk", "en-my", "en-ph",
    "de-de", "de-at", "de-ch", "fr-fr", "fr-be", "fr-ch",
    "es-es", "es-mx", "es-ar", "es-cl", "es-co", "es-pe",
    "it-it", "pt-pt", "pt-br", "nl-nl", "nl-be", "da-dk", "sv-se", "fi-fi",
    "pl-pl", "cs-cz", "el-gr", "ru-ru", "tr-tr", "ja-jp", "ko-kr",
]

TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
KEY_RE = re.compile(r"""(?:name|property)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
VAL_RE = re.compile(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def parse_meta(html):
    out = {}
    for tag in TAG_RE.findall(html):
        k, v = KEY_RE.search(tag), VAL_RE.search(tag)
        if k and v:
            out[(k.group(1) or k.group(2)).lower()] = v.group(1) or v.group(2) or ""
    return out


def parse_page(html):
    meta = parse_meta(html)
    title_tag = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = meta.get("og:title") or (title_tag.group(1).strip() if title_tag else "")
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return {
        "status": "ok",
        "ts": meta.get("publish_date_timestamp"),
        "title": title,
        "desc": meta.get("description") or meta.get("og:description") or "",
        "hash": hashlib.sha1(text.encode()).hexdigest(),
    }


def fetch(region):
    url = URL.format(region=region)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "replace")
            final = r.geturl()
    except urllib.error.HTTPError as e:
        return region, {"status": "missing" if e.code in (404, 410) else f"http {e.code}"}
    except Exception as e:  # network hiccup, don't treat as a change
        return region, {"status": f"error: {e}"}
    if "this-month-on-playstation" not in final:  # redirected away, page doesn't exist
        return region, {"status": "missing"}
    return region, parse_page(html)


def fmt_awst(ms):
    if not ms or not ms.isdigit():
        return "unknown"
    return f"{datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(LOCAL_TZ):%d %b %I:%M %p}"


def fmt_ts(ms):
    if not ms or not ms.isdigit():
        return "unknown"
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    return f"{dt:%d %b %Y %H:%M} UTC / {dt.astimezone(LOCAL_TZ):%d %b %Y %I:%M %p} AWST"


def diff(old, new, short=False):
    """Return a list of human-readable changes, empty if nothing meaningful changed."""
    changes = []
    if new["ts"] and new["ts"] != old.get("ts"):
        if short:
            changes.append(f"republished {fmt_awst(old.get('ts'))} -> {fmt_awst(new['ts'])} AWST")
        else:
            changes.append(f"republished: {fmt_ts(old.get('ts'))} -> {fmt_ts(new['ts'])}")
    if new["title"] != old.get("title"):
        changes.append(f"title: {old.get('title')!r} -> {new['title']!r}")
    if new["desc"] != old.get("desc"):
        changes.append("meta description changed")
    if not new["ts"] and new["hash"] != old.get("hash"):  # fallback if no timestamp meta
        changes.append("page content changed")
    return changes


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def run_once(regions):
    state = load_state()
    first_run = not state
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, regions))

    bad = sum(1 for _, n in results if n["status"] != "ok")
    outage = bad > len(regions) * OUTAGE_SHARE
    if outage:
        print(f"{bad}/{len(regions)} regions failed this run, looks like an outage, ignoring the failures",
              file=sys.stderr)

    updates, appeared = [], []  # updates: (region, [changes]) | appeared: [region]
    for region, new in results:
        old = state.get(region)
        if new["status"] != "ok":
            if outage:
                continue
            if new["status"] == "missing":
                if old and old.get("status") == "ok":
                    old["miss"] = old.get("miss", 0) + 1  # a live page has to stay missing before we believe it
                    if old["miss"] >= MISS_LIMIT:
                        old.pop("miss")
                        state[region] = dict(old, status="missing", was_live=True)
                elif not old:
                    state[region] = {"status": "missing"}
            else:
                print(f"[{region}] {new['status']} (skipped)", file=sys.stderr)
            continue
        if old and old.get("status") == "ok":
            old.pop("miss", None)
            changes = diff(old, new, short=True)
            if changes:
                updates.append((region, changes))
        elif old and old.get("status") == "missing" and not first_run:
            if old.get("was_live"):  # was live before, so compare with what we last saw
                changes = diff(old, new, short=True)
                if changes:
                    updates.append((region, changes))
            else:
                appeared.append(region)
        state[region] = new

    STATE_FILE.write_text(json.dumps(state, indent=2))
    ok = sum(1 for _, n in results if n["status"] == "ok")
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M")
    if first_run:
        print(f"[{stamp}] baseline saved for {ok}/{len(regions)} regions")
    elif updates or appeared:
        compact = len(updates) + len(appeared) > 8  # lots of regions at once: one short line each, no links
        out = [f"PlayStation 'This Month' update detected ({stamp} AWST)"]
        for region, changes in updates:
            if compact:
                out.append(f"**{region}** " + "; ".join(changes))
            else:
                out += [f"**{region}** updated"] + [f"  {c}" for c in changes] + [f"  {URL.format(region=region)}"]
        for region in appeared:
            out.append(f"**{region}** page just appeared" + ("" if compact else f"\n  {URL.format(region=region)}"))
        if compact:
            out.append("Page: " + URL.format(region="<region>"))
        send_alerts([{"text": "\n".join(out), "files": []}], PING_TEXT)
    else:
        print(f"[{stamp}] no changes ({ok} regions live" + (", outage ignored" if outage else "") + ")")


def show_state():
    for region, s in sorted(load_state().items()):
        if s.get("status") == "ok":
            print(f"{region:6} {fmt_ts(s['ts'])}  | {s['title']}")
        else:
            print(f"{region:6} {s.get('status')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=float, metavar="MINUTES")
    ap.add_argument("--regions", nargs="+")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        return show_state()
    regions = a.regions or REGIONS
    while True:
        run_once(regions)
        if not a.loop:
            break
        time.sleep(a.loop * 60)


if __name__ == "__main__":
    main()
