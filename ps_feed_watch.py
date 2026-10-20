#!/usr/bin/env python3
"""
Watches RSS feeds and pings Discord when a new post shows up (or an existing post is edited).
Add or remove feeds in FEEDS below. Stdlib only, no installs.
Needs ps_common.py in the same folder.

Usage:  python ps_feed_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

New post  -> summary + the post attached as an HTML page you can open in a browser.
Edited    -> summary of what changed + before and after HTML previews with differences highlighted.
First run just saves the posts currently in each feed as a baseline.
"""
import hashlib
import html
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from ps_common import UA, change_alert, cut, esc, file_stub, image_urls, send_alerts, to_lines

FEEDS = {
    "blog-gta6-tag": "https://blog.playstation.com/tag/grand-theft-auto-vi/feed",
    "blog-gta6-search": "https://blog.playstation.com/feed/?s=grand+theft+auto+vi",
}

STATE_FILE = Path(__file__).with_name("ps_feed_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone
ALERT_ON_EDITS = True     # also alert when an already-seen post is edited
MAX_SEEN = 500            # how many post ids to remember per feed

CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
# boilerplate text that shows up around every image on the blog
FEED_IGNORE = [re.compile(r"^(View and download image|Download the image|Close|Download this image|Play\s+Video)$")]

POST_CSS = """
body{margin:0;font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:#fff;color:#1c1c21}
a{color:#0a58ca}
@media (prefers-color-scheme:dark){body{background:#16161a;color:#ececf1}a{color:#6ea8ff}.bar{background:#202026!important}}
.bar{position:sticky;top:0;padding:10px 18px;background:#f4f4f7;border-bottom:4px solid #238c42;font-size:14px}
.bar b{background:#238c42;color:#fff;padding:3px 10px;border-radius:6px;margin-right:10px;letter-spacing:.06em}
article{max-width:820px;margin:0 auto;padding:18px}
img{max-width:100%;height:auto}
figure{margin:14px 0}
.meta{color:#6b6b76;font-size:14px}
.ps-image-modal,.youtube-video__iframe,.screen-reader-text,svg,button{display:none}
.youtube-video__link::after{content:"▶ Watch on YouTube";display:block;margin:4px 0}
"""


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
        body = it.findtext(CONTENT) or ""
        posts.append({
            "id": link or guid or title,
            "title": title,
            "link": link,
            "pub": (it.findtext("pubDate") or "").strip(),
            "cats": [c.text.strip() for c in it.findall("category") if c.text],
            "desc": desc,
            "body": body,
            "hash": hashlib.sha1((title + "\n" + (body or desc)).encode()).hexdigest(),
        })
    return posts


def body_snapshot(body):
    """Post body in the same line format the page watcher uses, so it can be diffed the same way."""
    return "\n".join([f"[img] {u}" for u in image_urls(body)] + to_lines(body, FEED_IGNORE))


def clean_body(body):
    """Make the post HTML work as a standalone file (lazy-loaded images, scripts)."""
    body = re.sub(r"<script\b.*?</script>", "", body, flags=re.S | re.I)

    def fix(m):
        tag = m.group(0)
        ds = re.search(r'\sdata-src="([^"]+)"', tag)
        if ds:
            real = ds.group(1)
            if re.search(r'\ssrc="', tag):
                tag = re.sub(r'\ssrc="[^"]*"', lambda _: f' src="{real}"', tag, count=1)
            else:
                tag = tag.replace("<img", f'<img src="{real}"', 1)
        return tag
    return re.sub(r"<img\b[^>]*>", fix, body, flags=re.I)


def post_page(p, label, stamp):
    meta = " | ".join(x for x in [p["pub"], ", ".join(p["cats"][:6])] if x)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<base href="https://blog.playstation.com/"><title>{esc(p['title'])}</title><style>{POST_CSS}</style></head>
<body><div class="bar"><b>{esc(label)}</b><a href="{esc(p['link'])}">{esc(p['link'])}</a> &nbsp; {esc(stamp)}</div>
<article><h1>{esc(p['title'])}</h1><p class="meta">{esc(meta)}</p>{clean_body(p['body']) or '<p>(no content in feed)</p>'}</article>
</body></html>
""".encode()


def slug(p):
    parts = [x for x in urlsplit(p["link"]).path.split("/") if x]
    return file_stub(parts[-1] if parts else p["title"])


def new_post_alert(feed, p, stamp):
    out = [f"**New PlayStation Blog post** ({feed}, {stamp})", p["title"], p["link"]]
    meta = " | ".join(x for x in [p["pub"], ", ".join(p["cats"][:6])] if x)
    if meta:
        out.append(meta)
    if p["desc"]:
        out.append(cut(p["desc"], 220))
    files = []
    if p["body"]:
        out += ["", "Attached: the post as a page (download and open in a browser)."]
        files = [(f"{slug(p)}.html", post_page(p, "NEW POST", stamp))]
    return {"text": "\n".join(out)[:1850], "files": files}


def edited_post_alert(feed, p, old_body, stamp):
    if not old_body:  # post was saved before we kept bodies, so there's nothing to compare against
        a = new_post_alert(feed, p, stamp)
        a["text"] = a["text"].replace("**New PlayStation Blog post**", "**PlayStation Blog post edited**", 1)
        return a
    return change_alert(cut(p["title"], 90), p["link"], stamp, body_snapshot(old_body), body_snapshot(p["body"]),
                        "blog post edited", note=f"Feed: {feed}", stub=slug(p),
                        raw_before=post_page(dict(p, body=old_body), "BEFORE", stamp).decode(),
                        raw_after=post_page(p, "AFTER", stamp).decode(), prepared=True)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def main():
    state = load_state()
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M") + " AWST"
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
            state[name] = {"items": {p["id"]: {"title": p["title"], "hash": p["hash"], "body": p["body"]}
                                     for p in posts}}
            baselined.append(f"{name} ({len(posts)} posts)")
            continue
        for p in posts:
            prev = seen.get(p["id"])
            if prev is None:
                if p["id"] not in alerted_ids:
                    alerts.append(new_post_alert(name, p, stamp))
                    alerted_ids.add(p["id"])
            elif ALERT_ON_EDITS and prev.get("hash") != p["hash"]:
                if p["id"] not in alerted_ids:
                    alerts.append(edited_post_alert(name, p, prev.get("body"), stamp))
                    alerted_ids.add(p["id"])
            seen[p["id"]] = {"title": p["title"], "hash": p["hash"], "body": p["body"]}
        current = {p["id"] for p in posts}
        for pid, v in seen.items():  # only keep bodies for posts still in the feed
            if pid not in current:
                v.pop("body", None)
        while len(seen) > MAX_SEEN:  # drop the oldest remembered ids
            seen.pop(next(iter(seen)))

    STATE_FILE.write_text(json.dumps(state, indent=2))
    if alerts:
        send_alerts(alerts, PING_TEXT)
    elif baselined:
        print(f"[{stamp}] baseline saved for: {', '.join(baselined)}")
    else:
        print(f"[{stamp}] no new posts")


if __name__ == "__main__":
    main()
