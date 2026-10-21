#!/usr/bin/env python3
"""
Watches RSS / Atom feeds and pings Discord when a new post shows up (or an existing post is edited).
Add or remove feeds in FEEDS below. Stdlib only, no installs.
Needs ps_common.py in the same folder.

Usage:  python ps_feed_watch.py
Optional: set DISCORD_WEBHOOK_URL to get alerts in Discord.

Each feed lists candidate feed URLs (the first one that works is remembered) and optionally a "page" to
auto-discover the feed from if none of them work.
New post  -> summary + the post attached as an HTML page, saved to the Wayback Machine.
Edited    -> summary of what changed + exact before and after copies of the post.
Posts mentioning GTA / Rockstar / Take-Two etc. get a [GTA match] tag.
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
from urllib.parse import urljoin, urlsplit

from ps_common import (UA, change_alert, cut, esc, file_stub, image_urls, send_alerts, to_lines,
                       wayback_line, wayback_many)

FEEDS = {
    "blog-gta6-tag": {
        "label": "PlayStation Blog",
        "urls": ["https://blog.playstation.com/tag/grand-theft-auto-vi/feed"],
    },
    "blog-gta6-search": {
        "label": "PlayStation Blog",
        "urls": ["https://blog.playstation.com/feed/?s=grand+theft+auto+vi"],
    },
    "vector-blog": {   # WordPress site: standard feed paths, plus auto-discovery from the blog page
        "label": "Vector Media blog",
        "urls": ["https://vectormedia.com/feed/", "https://vectormedia.com/blog/feed/"],
        "page": "https://vectormedia.com/blog/",
    },
    "vector-work": {   # case studies are a custom post type
        "label": "Vector Media case study",
        "urls": ["https://vectormedia.com/work/feed/", "https://vectormedia.com/feed/?post_type=work"],
        "page": "https://vectormedia.com/work/",
    },
}

# posts matching this get a [GTA match] tag in the alert
HIGHLIGHT = re.compile(
    r"\bgta\b|gta[\s_-]*(?:vi|6)\b|gtavi|grand[\s_-]*theft[\s_-]*auto|vice[\s_-]*city|leonida|rockstar|take[\s-]*two",
    re.I)

STATE_FILE = Path(__file__).with_name("ps_feed_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone
ALERT_ON_EDITS = True     # also alert when an already-seen post is edited
ARCHIVE_WAYBACK = False   # save new / edited posts to the Wayback Machine (off: only storefronts are archived)
WAYBACK_MAX_PER_RUN = 6
MAX_SEEN = 500            # how many post ids to remember per feed

CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"
ATOM = "{http://www.w3.org/2005/Atom}"
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
        "User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return "ok", r.read()
    except urllib.error.HTTPError as e:
        return f"http {e.code}", b""
    except Exception as e:  # network hiccup: skip, don't treat as a change
        return f"error: {e}", b""


def _text(el):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(el or ""))).strip()


def parse_feed(data):
    """Returns a list of posts, or None if this isn't a valid RSS/Atom feed (blocked page, 404 page etc.)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    kind = root.tag.rsplit("}", 1)[-1].lower()
    posts = []
    if kind == "feed":  # Atom
        for it in root.findall(ATOM + "entry"):
            link = next((l.get("href", "") for l in it.findall(ATOM + "link") if l.get("rel", "alternate") == "alternate"), "")
            title = (it.findtext(ATOM + "title") or "").strip()
            body = it.findtext(ATOM + "content") or ""
            desc = _text(it.findtext(ATOM + "summary") or body)
            posts.append({
                "id": link or (it.findtext(ATOM + "id") or "").strip() or title, "title": title, "link": link,
                "pub": (it.findtext(ATOM + "published") or it.findtext(ATOM + "updated") or "").strip(),
                "cats": [c.get("term") for c in it.findall(ATOM + "category") if c.get("term")],
                "desc": desc, "body": body,
                "hash": hashlib.sha1((title + "\n" + (body or desc)).encode()).hexdigest(),
            })
        return posts
    if root.find("channel") is None:
        return None
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or "").strip()
        title = (it.findtext("title") or "").strip()
        desc = _text(it.findtext("description"))
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


FEED_LINK = re.compile(r"<link\b[^>]*>", re.I)


def find_feed_links(page_html, base):
    """Feed URLs a page advertises in its <head> (<link rel="alternate" type="application/rss+xml" ...>)."""
    out = []
    for tag in FEED_LINK.findall(page_html):
        if not re.search(r'rel\s*=\s*["\']?alternate', tag, re.I) or not re.search(r"application/(rss|atom)\+xml", tag, re.I):
            continue
        m = re.search(r'href\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag, re.I)
        if m:
            u = urljoin(base, html.unescape(m.group(1) or m.group(2)))
            if "/comments" not in u and u not in out:
                out.append(u)
    return out


def load_posts(entry, remembered):
    """Try the remembered feed URL, then the candidates, then whatever the page advertises."""
    tried = []
    for u in ([remembered] if remembered else []) + [u for u in entry["urls"] if u != remembered]:
        status, data = fetch(u)
        posts = parse_feed(data) if status == "ok" else None
        if posts is not None:
            return posts, u
        tried.append(u)
    page = entry.get("page")
    if page:
        status, data = fetch(page)
        if status == "ok":
            for u in find_feed_links(data.decode("utf-8", "replace"), page):
                if u in tried:
                    continue
                st, d = fetch(u)
                posts = parse_feed(d) if st == "ok" else None
                if posts is not None:
                    return posts, u
    return None, None


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
    sp = urlsplit(p["link"])
    base = f"{sp.scheme}://{sp.netloc}/" if sp.netloc else ""
    meta = " | ".join(x for x in [p["pub"], ", ".join(p["cats"][:6])] if x)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<base href="{esc(base)}"><title>{esc(p['title'])}</title><style>{POST_CSS}</style></head>
<body><div class="bar"><b>{esc(label)}</b><a href="{esc(p['link'])}">{esc(p['link'])}</a> &nbsp; {esc(stamp)}</div>
<article><h1>{esc(p['title'])}</h1><p class="meta">{esc(meta)}</p>{clean_body(p['body']) or '<p>(no content in feed)</p>'}</article>
</body></html>
""".encode()


def slug(p):
    parts = [x for x in urlsplit(p["link"]).path.split("/") if x]
    return file_stub(parts[-1] if parts else p["title"])


def gta_tag(p):
    m = HIGHLIGHT.search(" ".join([p["title"], p["desc"], _text(p["body"]), " ".join(p["cats"])]))
    return (f"[GTA match: {m.group(0).lower()}] ", m.group(0).lower()) if m else ("", "")


def new_post_alert(feed, label, p, stamp):
    tag, _ = gta_tag(p)
    out = [f"{tag}**New {label} post** ({feed}, {stamp})", p["title"], p["link"]]
    meta = " | ".join(x for x in [p["pub"], ", ".join(p["cats"][:6])] if x)
    if meta:
        out.append(meta)
    if p["desc"]:
        out.append(cut(p["desc"], 220))
    files = []
    if p["body"]:
        out += ["", "Attached: the post as a page (download and open in a browser)."]
        files = [(f"{slug(p)}.html", post_page(p, "NEW POST", stamp))]
    return {"text": "\n".join(out)[:1650], "files": files}


def edited_post_alert(feed, label, p, old_body, stamp):
    tag, _ = gta_tag(p)
    if not old_body:  # post was saved before we kept bodies, so there's nothing to compare against
        a = new_post_alert(feed, label, p, stamp)
        a["text"] = a["text"].replace(f"**New {label} post**", f"**{label} post edited**", 1)
        return a
    a = change_alert(cut(p["title"], 90), p["link"], stamp, body_snapshot(old_body), body_snapshot(p["body"]),
                     f"{label} post edited", note=f"Feed: {feed}", stub=slug(p),
                     raw_before=post_page(dict(p, body=old_body), "BEFORE", stamp).decode(),
                     raw_after=post_page(p, "AFTER", stamp).decode(), prepared=True)
    a["text"] = tag + a["text"]
    return a


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def main():
    state = load_state()
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M") + " AWST"
    alerts, baselined = [], []  # alerts: (alert dict, post link)
    alerted_ids = set()  # same post can show up in several feeds, only alert once per run
    for name, entry in FEEDS.items():
        old_state = state.get(name, {})
        posts, used = load_posts(entry, old_state.get("feed_url"))
        if posts is None:
            print(f"[{name}] no working feed found, probably blocked or not a feed (skipped)", file=sys.stderr)
            continue
        label = entry["label"]
        seen = old_state.get("items")
        if seen is None:  # first run for this feed
            state[name] = {"feed_url": used,
                           "items": {p["id"]: {"title": p["title"], "hash": p["hash"], "body": p["body"]} for p in posts}}
            baselined.append(f"{name} ({len(posts)} posts via {used})")
            continue
        state[name]["feed_url"] = used
        for p in posts:
            prev = seen.get(p["id"])
            if prev is None:
                if p["id"] not in alerted_ids:
                    alerts.append((new_post_alert(name, label, p, stamp), p["link"]))
                    alerted_ids.add(p["id"])
            elif ALERT_ON_EDITS and prev.get("hash") != p["hash"]:
                if p["id"] not in alerted_ids:
                    alerts.append((edited_post_alert(name, label, p, prev.get("body"), stamp), p["link"]))
                    alerted_ids.add(p["id"])
            seen[p["id"]] = {"title": p["title"], "hash": p["hash"], "body": p["body"]}
        current = {p["id"] for p in posts}
        for pid, v in seen.items():  # only keep bodies for posts still in the feed
            if pid not in current:
                v.pop("body", None)
        while len(seen) > MAX_SEEN:  # drop the oldest remembered ids
            seen.pop(next(iter(seen)))

    if ARCHIVE_WAYBACK and alerts:
        results = wayback_many([link for _, link in alerts if link], limit=WAYBACK_MAX_PER_RUN)
        for alert, link in alerts:
            if link in results:
                got, why = results[link]
                alert["text"] += "\n" + wayback_line(None, got, why)

    STATE_FILE.write_text(json.dumps(state, indent=2))
    if alerts:
        send_alerts([a for a, _ in alerts], PING_TEXT)
    elif baselined:
        print(f"[{stamp}] baseline saved for: {', '.join(baselined)}")
    else:
        print(f"[{stamp}] no new posts")


if __name__ == "__main__":
    main()
