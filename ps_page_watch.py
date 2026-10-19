#!/usr/bin/env python3
"""
Watches specific web pages and pings Discord when they change.
Add or remove pages in WATCH / KEYWORD_WATCH below. Stdlib only, no installs.

Usage:  python ps_page_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

Two kinds of watching:
  WATCH          full-page: meta tags, every image URL and the visible text. Any change alerts.
  KEYWORD_WATCH  only GTA-related stuff (see KEYWORDS): links, text lines and image URLs that
                 mention it. Alerts when those appear or disappear, ignores everything else.

Pages that return 404 (not live yet) are remembered and you get a "now LIVE" alert the moment
they appear. First run just saves a baseline.
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
from urllib.parse import urljoin

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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CONFIRM = False       # True = require the change on 2 consecutive runs before alerting
MAX_DIFF = 14         # max changed text lines shown per alert
MAX_IMG = 10          # max changed image lines shown per alert
SNAP_V = 2            # bump when the snapshot format changes (triggers a silent re-baseline)
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
A_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*(?:"([^"]*)"|'([^']*)')[^>]*>(.*?)</a>""", re.I | re.S)
BLOCK_RE = re.compile(
    r"</?(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|section|article|header|footer|nav|button|label|option)\b[^>]*>",
    re.I)
IMG_RE = re.compile(
    r"""https?://[^\s"'<>()\\]+?\.(?:png|jpe?g|webp|gif|avif|svg)\b"""
    r"""|https?://(?:image\.api|gmedia)\.playstation\.com[^\s"'<>()\\]*""", re.I)


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


def image_urls(page):
    """Every image URL in the raw page (including ones inside embedded JSON), minus query strings."""
    s = page.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&")
    urls = {m.group(0).split("?")[0].split("#")[0].rstrip(".,;") for m in IMG_RE.finditer(s)}
    return sorted(urls)


def keyword_lines(page, url):
    """Only the GTA-related links, text lines and image URLs on a page."""
    found = set()
    for m in A_RE.finditer(page):
        href = html.unescape(m.group(1) or m.group(2) or "").strip()
        text = re.sub(r"<[^>]+>", " ", m.group(3))
        text = re.sub(r"\s+", " ", html.unescape(text)).strip()
        if KEYWORDS.search(text) or KEYWORDS.search(href):
            found.add(f"[link] {text or '(no text)'} -> {urljoin(url, href)}")
    for line in to_lines(page):
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
    lines += to_lines(body)
    text = "\n".join(lines)
    if len(lines) < 5:  # basically empty page (blocked / JS-only), don't trust it
        return {"status": "empty"}
    return {"status": "ok", "v": SNAP_V, "hash": hashlib.sha1(text.encode()).hexdigest(),
            "text": text[:MAX_STATE]}


def keyword_snapshot(url):
    status, body = fetch(url)
    if status != "ok":
        return {"status": status}
    if len(to_lines(body)) < 15:  # blocked / JS-only shell, don't trust it
        return {"status": "empty"}
    text = "\n".join(keyword_lines(body, url))
    return {"status": "ok", "v": SNAP_V, "mode": "keywords",
            "hash": hashlib.sha1(text.encode()).hexdigest(), "text": text[:MAX_STATE]}


def describe(name, url, old, new, kind="changed"):
    out = [f"**{name}** {kind}", url]
    po, pn = set(PRICE.findall(old)), set(PRICE.findall(new))
    if po != pn:
        out.append(f"prices: {', '.join(sorted(po)) or '-'} -> {', '.join(sorted(pn)) or '-'}")
    diff = [l for l in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
            if l and l[0] in "+-" and not l.startswith(("+++", "---"))]
    imgs = [l for l in diff if l[1:].startswith("[img] ")]
    rest = [l for l in diff if not l[1:].startswith("[img] ")]
    for l in rest[:MAX_DIFF]:
        out.append("  " + l[:200])
    if len(rest) > MAX_DIFF:
        out.append(f"  ... and {len(rest) - MAX_DIFF} more changed lines")
    for l in imgs[:MAX_IMG]:
        out.append("  " + l[:240])
    if len(imgs) > MAX_IMG:
        out.append(f"  ... and {len(imgs) - MAX_IMG} more image changes")
    return "\n".join(out)


def preview(snap):
    lines = snap.get("text", "").splitlines()
    meta = [l for l in lines if l.startswith(("[meta] title:", "[meta] description:"))]
    body = [l for l in lines if not l.startswith(("[meta]", "[img]"))][:8]
    imgs = [l for l in lines if l.startswith("[img]")][:3]
    return "\n".join("  " + l[:200] for l in meta + body + imgs)


def notify(lines):
    msg = "\n".join(lines)
    print(msg, flush=True)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            payload = {"content": ((PING_TEXT + "\n") if PING_TEXT else "") + msg[:1880]}
            if PING_TEXT:
                payload["allowed_mentions"] = {"parse": ["everyone"]}
            body = json.dumps(payload).encode()
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


def targets():
    for name, url in WATCH.items():
        yield name, url, snapshot, "changed"
    for name, url in KEYWORD_WATCH.items():
        yield name, url, keyword_snapshot, "has GTA-related changes"


def main():
    state = load_state()
    alerts, baselined, notes, pending = [], [], [], []
    for name, url, snap, kind in targets():
        new = snap(url)
        status = new["status"]
        old = state.get(name)

        if status in ("http 404", "http 410"):  # page doesn't exist (yet)
            if old and old.get("status") == "ok":
                old["miss"] = old.get("miss", 0) + 1  # needs 2 misses in a row, avoids blips
                if old["miss"] >= 2:
                    alerts.append(f"**{name}** is gone ({status})\n{url}")
                    state[name] = {"status": "missing"}
            elif not old:
                state[name] = {"status": "missing"}
                notes.append(f"{name}: not live yet (404), will alert when it appears")
            continue
        if status != "ok":
            print(f"[{name}] {status} (skipped)", file=sys.stderr)
            continue

        if old and old.get("status") == "missing":  # it just appeared
            alerts.append(f"**{name}** is now LIVE\n{url}\n{preview(new)}")
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
        alerts.append(describe(name, url, old["text"], new["text"], kind))
        state[name] = new

    STATE_FILE.write_text(json.dumps(state, indent=2))
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M")
    if alerts:
        notify([f"Page change detected ({stamp} AWST)"] + alerts)
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
