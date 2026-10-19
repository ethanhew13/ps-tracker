#!/usr/bin/env python3
"""
Watches specific web pages and pings Discord when their content changes.
Add or remove pages in WATCH below. Stdlib only, no installs.

Usage:  python ps_page_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

How it decides something changed:
  - grabs the page's meta tags (title, description, release date, etc.) and its visible text
  - compares to the last saved copy and shows you exactly which lines changed
  - a change has to show up on 2 runs in a row before it alerts (CONFIRM = True),
    which filters out one-off glitches and flapping content
First run just saves a baseline.
"""
import difflib
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

WATCH = {
    "ps-gta6-page": "https://www.playstation.com/en-us/games/grand-theft-auto-vi/",
    "store-gta6-standard": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVISTANDARD001",
    "store-gta6-ultimate": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVIULTIMATE001",
}

STATE_FILE = Path(__file__).with_name("ps_page_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CONFIRM = True        # require the change on 2 consecutive runs before alerting
MAX_DIFF = 14         # max changed lines shown per alert
MAX_STATE = 150_000   # max chars of page text kept in the state file

# meta tags that change on every deploy and aren't real content changes
IGNORE_META = {"wca-ssr-version", "wca-theme", "viewport", "next-head-count",
               "content-page-ref", "csrf-token"}
# visible-text lines to ignore (regex). Add noisy stuff here if you get false alerts.
IGNORE_LINES = [re.compile(p) for p in [r"^©"]]

PRICE = re.compile(r"\$\d[\d,]*\.\d\d")
TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
KEY_RE = re.compile(r"""(?:name|property)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
VAL_RE = re.compile(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
BLOCK_RE = re.compile(
    r"</?(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|section|article|header|footer|nav|button|label|option)\b[^>]*>",
    re.I)


def parse_meta(page):
    out = {}
    for tag in TAG_RE.findall(page):
        k, v = KEY_RE.search(tag), VAL_RE.search(tag)
        if k and v:
            out[(k.group(1) or k.group(2)).lower()] = html.unescape(v.group(1) or v.group(2) or "")
    return out


def to_lines(page):
    s = re.sub(r"<(script|style|noscript|svg|template)\b.*?</\1>", " ", page, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = BLOCK_RE.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    lines = []
    for raw in html.unescape(s).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or any(p.search(line) for p in IGNORE_LINES):
            continue
        if lines and lines[-1] == line:  # buttons etc. are often rendered twice
            continue
        lines.append(line)
    return lines


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
    lines += to_lines(body)
    text = "\n".join(lines)
    if len(lines) < 5:  # basically empty page (blocked / JS-only), don't trust it
        return {"status": "empty"}
    return {"status": "ok", "hash": hashlib.sha1(text.encode()).hexdigest(), "text": text[:MAX_STATE]}


def describe(name, url, old, new):
    out = [f"**{name}** changed", url]
    po, pn = set(PRICE.findall(old)), set(PRICE.findall(new))
    if po != pn:
        out.append(f"prices: {', '.join(sorted(po)) or '-'} -> {', '.join(sorted(pn)) or '-'}")
    diff = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if l and l[0] in "+-" and not l.startswith(("+++", "---"))]
    for l in diff[:MAX_DIFF]:
        out.append("  " + l[:160])
    if len(diff) > MAX_DIFF:
        out.append(f"  ... and {len(diff) - MAX_DIFF} more changed lines")
    return "\n".join(out)


def notify(lines):
    msg = "\n".join(lines)
    print(msg, flush=True)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            body = json.dumps({"content": msg[:1900]}).encode()
            req = urllib.request.Request(
                hook, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"discord webhook failed: {e}", file=sys.stderr)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def main():
    state = load_state()
    alerts, baselined, pending = [], [], []
    for name, url in WATCH.items():
        new = snapshot(url)
        if new["status"] != "ok":
            print(f"[{name}] {new['status']} (skipped)", file=sys.stderr)
            continue
        old = state.get(name)
        if not old or old.get("status") != "ok":
            state[name] = new
            baselined.append(name)
            continue
        if new["hash"] == old["hash"]:
            old.pop("pending", None)  # change flipped back, ignore it
            continue
        if CONFIRM and old.get("pending") != new["hash"]:
            old["pending"] = new["hash"]  # first sighting, wait for the next run
            pending.append(name)
            continue
        alerts.append(describe(name, url, old["text"], new["text"]))
        state[name] = new

    STATE_FILE.write_text(json.dumps(state, indent=2))
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M")
    if alerts:
        notify([f"Page change detected ({stamp} AWST)"] + alerts)
    elif baselined:
        print(f"[{stamp}] baseline saved for: {', '.join(baselined)}")
    else:
        extra = f" (unconfirmed change on: {', '.join(pending)})" if pending else ""
        print(f"[{stamp}] no changes{extra}")


if __name__ == "__main__":
    main()
