#!/usr/bin/env python3
"""
Watches RSS feeds and pings Discord when a new post shows up (or an existing post is edited).
Add or remove feeds in FEEDS below. Stdlib only, no installs.

Usage:  python ps_feed_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

First run just saves the posts currently in each feed as a baseline.
"""
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEEDS = {
    "blog-gta6-tag": "https://blog.playstation.com/tag/grand-theft-auto-vi/feed",
    "blog-gta6-search": "https://blog.playstation.com/feed/?s=grand+theft+auto+vi",
}

STATE_FILE = Path(__file__).with_name("ps_feed_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone
ALERT_ON_EDITS = True     # also alert when an already-seen post is edited
MAX_SEEN = 500            # how many post ids to remember per feed
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return "ok", r.read()
    except urllib.error.HTTPError as e:
        return f"http {e.code}", b""
    except Exception as e:  # network hiccup: skip, don't treat as a change
        return f"error: {e}", b""


def parse_feed(data):
    """Returns a list of posts, or None if this isn't a valid RSS feed (blocked page etc.)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    if root.find("channel") is None:
        return None
    posts = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", html.unescape(it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        body = it.findtext(CONTENT) or desc
        posts.append({
            "id": link or guid or title,
            "title": title,
            "link": link,
            "pub": (it.findtext("pubDate") or "").strip(),
            "cats": [c.text.strip() for c in it.findall("category") if c.text],
            "desc": desc,
            "hash": hashlib.sha1((title + "\n" + body).encode()).hexdigest(),
        })
    return posts


def format_post(kind, feed, p):
    out = [f"**{kind}** ({feed})", p["title"], p["link"]]
    meta = " | ".join(x for x in [p["pub"], ", ".join(p["cats"][:6])] if x)
    if meta:
        out.append(meta)
    if p["desc"]:
        out.append(p["desc"][:220] + ("..." if len(p["desc"]) > 220 else ""))
    return "\n".join(out)


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


def main():
    state = load_state()
    alerts, baselined = [], []
    alerted_ids = set()  # same post can show up in several feeds, only alert once per run
    for name, url in FEEDS.items():
        status, data = fetch(url)
        if status != "ok":
            print(f"[{name}] {status} (skipped)", file=sys.stderr)
            continue
        posts = parse_feed(data)
        if posts is None:
            print(f"[{name}] not a valid feed, probably blocked (skipped)", file=sys.stderr)
            continue
        seen = state.get(name, {}).get("items")
        if seen is None:  # first run for this feed
            state[name] = {"items": {p["id"]: {"title": p["title"], "hash": p["hash"]} for p in posts}}
            baselined.append(f"{name} ({len(posts)} posts)")
            continue
        for p in posts:
            prev = seen.get(p["id"])
            if prev is None:
                if p["id"] not in alerted_ids:
                    alerts.append(format_post("New PlayStation Blog post", name, p))
                    alerted_ids.add(p["id"])
            elif ALERT_ON_EDITS and prev.get("hash") != p["hash"]:
                if p["id"] not in alerted_ids:
                    alerts.append(format_post("PlayStation Blog post edited", name, p))
                    alerted_ids.add(p["id"])
            seen[p["id"]] = {"title": p["title"], "hash": p["hash"]}
        while len(seen) > MAX_SEEN:  # drop the oldest remembered ids
            seen.pop(next(iter(seen)))

    STATE_FILE.write_text(json.dumps(state, indent=2))
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M")
    if alerts:
        notify([f"Blog update detected ({stamp} AWST)"] + alerts)
    elif baselined:
        print(f"[{stamp}] baseline saved for: {', '.join(baselined)}")
    else:
        print(f"[{stamp}] no new posts")


if __name__ == "__main__":
    main()
