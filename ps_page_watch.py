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

Every alert is a readable summary plus two attached HTML files: an exact copy of the page before and
after (the last saved copy lives in snapshots/), with every image and the list of changes added at the
very end of each file. Rebuilt-preview fallback if no earlier copy exists. The
differences listed at the bottom, with the exact source lines. Prices are ignored. Changed images are attached to the alert too, and every image the pages
use is saved into an images/ folder next to this script (so the workflow commits it to the repo),
which keeps images viewable after PlayStation removes them. Every page is also saved to the Wayback
Machine (once when first seen, then again on every change) and the alert links the before/after captures,
but only for the storefront pages listed in WAYBACK_PAGES. Pages that return 404 (not live yet) are remembered and you get a
"now LIVE" alert the moment they appear. First run just saves a baseline.
"""
import hashlib
import html
import http.cookiejar
import io
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ps_common import (UA, change_alert, gone_alert, image_urls, live_alert, mask_prices, parse_meta,
                       parse_snapshot, send_alerts, sniff, to_lines, wayback_line, wayback_many)

WATCH = {
    "ps-gta6-page": "https://www.playstation.com/en-us/games/grand-theft-auto-vi/",
    "store-gta6-standard": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVISTANDARD001",
    "store-gta6-ultimate": "https://store.playstation.com/en-us/product/EP1004-PPSA01547_00-GTAVIULTIMATE001",
    "support-gta6-offer-terms": "https://www.playstation.com/en-us/support/games/gta-vi-offer-terms",
    "support-gta6-game": "https://www.playstation.com/en-us/support/games/gta-vi-game/",
    # first-party storefronts only (no third-party retailers); these are the pages saved to the Wayback Machine
    "store-au-standard": "https://store.playstation.com/en-au/product/EP1004-PPSA01547_00-GTAVISTANDARD001",
    "store-au-ultimate": "https://store.playstation.com/en-au/product/EP1004-PPSA01547_00-GTAVIULTIMATE001",
    "xbox-gta6-au": "https://www.xbox.com/en-AU/games/store/grand-theft-auto-vi/9p3h4968grsm",
    "xbox-gta6-au-2": "https://www.xbox.com/en-AU/games/store/grand-theft-auto-vi/9nl3wwnzlzzn",
    "nz-oflc-gta6": "https://www.classificationoffice.govt.nz/find-a-rating/quick-takes/grand-theft-auto-vi/",
    "vector-work": "https://vectormedia.com/work/",
    "vector-blog": "https://vectormedia.com/blog",
}

KEYWORD_WATCH = {
    "sitemap": "https://www.playstation.com/en-us/site-map/",
    "support-games-list": "https://www.playstation.com/en-us/support/games/?category=game-support&subCategory=support-for-specific-games",
}

# Links that redirect somewhere: tracks the redirect hop by hop plus one section of the page it lands on
# (the element the #fragment points at; "section" is the fallback id). Alerts if the redirect goes somewhere
# new, if a dedicated page appears, or if that section changes.
SECTION_WATCH = {
    "ps-gta6-features-link": {"url": "https://playstation.com/GTAVIfeatures", "lang": "en-AU", "section": "ps5-features"},
    # the same section on the AU page directly (the link sends Australians here, but our servers are in the US)
    "ps-gta6-features-au": {"url": "https://www.playstation.com/en-au/games/grand-theft-auto-vi/", "lang": "en-AU",
                            "section": "ps5-features"},
}

# What counts as "GTA stuff" on keyword pages. Edit freely.
KEYWORDS = re.compile(
    r"\bgta\b|gta[\s_-]*(?:vi|6)\b|gtavi|grand[\s_-]*theft[\s_-]*auto|vice[\s_-]*city|leonida",
    re.I)

STATE_FILE = Path(__file__).with_name("ps_page_state.json")
LOCAL_TZ = timezone(timedelta(hours=8))  # AWST, no DST
PING_TEXT = "@everyone"   # set to "" to stop pinging everyone

CONFIRM = False       # True = require the change on 2 consecutive runs before alerting
SNAP_V = 3            # bump when the snapshot format changes (triggers a silent re-baseline); 3 = prices ignored
MAX_STATE = 150_000   # max chars of page text kept in the state file

SNAP_DIR = Path(__file__).with_name("snapshots")   # last saved HTML of each page (the "before" copy)
MAX_RAW = 2_000_000          # pages bigger than this are not saved as exact copies

ARCHIVE_WAYBACK = True       # save storefront pages to the Wayback Machine (first sighting + every change)
WAYBACK_PAGES = {            # only these get saved; everything else is left alone
    "store-gta6-standard", "store-gta6-ultimate", "store-au-standard", "store-au-ultimate",
    "xbox-gta6-au", "xbox-gta6-au-2",
}
WAYBACK_MAX_PER_RUN = 6      # be polite: at most this many captures per run, the rest wait for the next run

ARCHIVE_IMAGES = True        # True = save every image into images/ (committed to the repo by the workflow)
IMAGE_DIR = Path(__file__).with_name("images")
MAX_IMG_BYTES = 8_000_000    # skip images bigger than this
MAX_DOWNLOADS_PER_RUN = 40   # archive in batches so the first run doesn't take forever
MAX_ARCHIVE_MB = 300         # stop saving new images once the archive reaches this size

# meta tags that change on every deploy and aren't real content changes
IGNORE_META = {"wca-ssr-version", "wca-theme", "viewport", "next-head-count",
               "content-page-ref", "csrf-token"}
# any meta tag whose name looks like a rotating token is ignored too
VOLATILE_META = re.compile(r"csrf|xsrf|nonce|token|session|request[-_]?id|trace[-_]?id|build[-_]?id", re.I)
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_chain(url, lang="en-AU", max_hops=8):
    """Follow redirects by hand so every hop is recorded.
    Returns (status, hops, final_url, html); hops = [(status, url, where it points or None, "http"/"meta"/"final")]."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _NoRedirect)
    hops, cur, seen = [], url, set()
    for _ in range(max_hops):
        if cur in seen:
            return "redirect loop", hops, cur, ""
        seen.add(cur)
        req = urllib.request.Request(cur, headers={"User-Agent": UA, "Accept-Language": f"{lang},en;q=0.8"})
        try:
            with opener.open(req, timeout=25) as r:
                status, loc, body = r.status, None, r.read()
        except urllib.error.HTTPError as e:
            status, loc = e.code, e.headers.get("Location")
            body = b"" if status in (301, 302, 303, 307, 308) else e.read()
        except Exception as e:
            return f"error: {e}", hops, cur, ""
        page = body.decode("utf-8", "replace")
        if status in (301, 302, 303, 307, 308) and loc:
            hops.append((status, cur, urljoin(cur, loc), "http"))
            cur = urljoin(cur, loc)
            continue
        if status == 200 and len(page) < 6000:  # a tiny page that just forwards you somewhere
            mr = re.search(r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["'][^"']*url=\s*([^"'>\s]+)""", page, re.I)
            if mr:
                nxt = urljoin(cur, html.unescape(mr.group(1)))
                hops.append((200, cur, nxt, "meta"))
                cur = nxt
                continue
        hops.append((status, cur, None, "final"))
        return ("ok" if status == 200 else f"http {status}"), hops, cur, page
    return "too many redirects", hops, cur, ""


def element_by_id(page, eid):
    """The HTML of the element with this id (including everything nested inside it), or None."""
    m = re.search(r"""<([A-Za-z][\w:-]*)\b[^>]*\sid\s*=\s*["']""" + re.escape(eid) + r"""["'][^>]*>""", page, re.I)
    if not m:
        return None
    tag, depth, pos = m.group(1).lower(), 1, m.end()
    for t in re.finditer(rf"<(/?){re.escape(tag)}\b[^>]*>", page[pos:], re.I):
        if t.group(1):
            depth -= 1
        elif not t.group(0).rstrip().endswith("/>"):
            depth += 1
        if depth == 0:
            return page[m.start():pos + t.end()]
    return page[m.start():m.start() + 30000]


def page_lines(body):
    meta = parse_meta(body)
    lines = [f"[meta] {k}: {v}" for k, v in sorted(meta.items()) if k not in IGNORE_META and not VOLATILE_META.search(k)]
    lines += [f"[img] {u}" for u in image_urls(body)]
    lines += to_lines(body, IGNORE_LINES)
    return lines


def section_snapshot(url, cfg):
    """Tracks where a link redirects (hop by hop) plus one section of the page it lands on (the element the
    #fragment points at). If that section can't be found, the whole landing page is tracked instead."""
    status, hops, final_url, body = fetch_chain(url, cfg.get("lang", "en-AU"))
    if status != "ok":
        return {"status": status}
    red = []
    for code, u, to, via in hops:
        red.append(f"[redirect] {'meta refresh' if via == 'meta' else code} {u} -> {to}" if to else f"[final] {code} {u}")
    frag = next((urlsplit(to).fragment.strip("/") for _, _, to, _ in hops if to and urlsplit(to).fragment), "")
    ids = [i for i in (frag, cfg.get("section", "")) if i]  # the redirect's own #fragment first, then our default
    eid, sec = "", None
    for i in ids:
        sec = element_by_id(body, i)
        if sec:
            eid = i
            break
    eid = eid or (ids[0] if ids else "")
    if sec:
        lines = red + [f"[section] #{eid}"] + [f"[img] {u}" for u in image_urls(sec)] + to_lines(sec, IGNORE_LINES)
    else:
        lines = red + [f"[section] #{eid or '?'} not found, tracking the whole page"] + page_lines(body)
        if len(lines) < 8:  # blocked / JS-only page
            return {"status": "empty"}
    text = "\n".join(lines)
    return {"status": "ok", "v": SNAP_V, "hash": hashlib.sha1(mask_prices(text).encode()).hexdigest(),
            "text": text[:MAX_STATE], "_raw": body}


def snapshot(url):
    status, body = fetch(url)
    if status != "ok":
        return {"status": status}
    lines = page_lines(body)
    text = "\n".join(lines)
    if len(lines) < 5:  # basically empty page (blocked / JS-only), don't trust it
        return {"status": "empty"}
    return {"status": "ok", "v": SNAP_V, "hash": hashlib.sha1(mask_prices(text).encode()).hexdigest(),
            "text": text[:MAX_STATE], "_raw": body}


def keyword_snapshot(url):
    status, body = fetch(url)
    if status != "ok":
        return {"status": status}
    if len(to_lines(body, IGNORE_LINES)) < 15:  # blocked / JS-only shell, don't trust it
        return {"status": "empty"}
    text = "\n".join(keyword_lines(body, url))
    return {"status": "ok", "v": SNAP_V, "mode": "keywords",
            "hash": hashlib.sha1(mask_prices(text).encode()).hexdigest(), "text": text[:MAX_STATE], "_raw": body}


def download(url):
    """Image bytes, or None if it can't be fetched, is too big, or isn't really an image."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = r.read(MAX_IMG_BYTES + 1)
    except Exception:
        return None
    if not data or len(data) > MAX_IMG_BYTES or sniff(data)[0] == "application/octet-stream":
        return None
    return data


def make_thumb(data, max_w=360):
    """Small version of an image for the gallery (needs Pillow; without it only tiny images are used as they are)."""
    if b"<svg" in data[:500].lower():
        return data if len(data) <= 40_000 else None
    try:
        from PIL import Image
    except ImportError:
        return data if len(data) <= 40_000 else None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        im.thumbnail((max_w, max_w * 2))
        if im.mode in ("RGBA", "LA", "P"):
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[3])
            im = bg
        else:
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=62, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


class Archive:
    """Saves every image we see into images/ (plus images/index.json) and hands out the bytes."""

    def __init__(self):
        self.index, self.cache, self.new_saved, self.dirty = {}, {}, 0, False
        if ARCHIVE_IMAGES:
            try:
                self.index = json.loads((IMAGE_DIR / "index.json").read_text())
            except Exception:
                self.index = {}
        self.total = sum(e.get("bytes", 0) for e in self.index.values())

    def get(self, url):
        if url in self.cache:
            return self.cache[url]
        e = self.index.get(url) or {}
        if e.get("file"):
            try:
                self.cache[url] = (IMAGE_DIR / e["file"]).read_bytes()
                return self.cache[url]
            except OSError:
                pass  # saved file went missing, try downloading again
        elif e.get("failed", 0) >= 3 or urlsplit(url).path in ("", "/"):
            return None  # gave up on this one (dead link or just a bare domain)
        data = download(url)
        if data is None:
            if ARCHIVE_IMAGES and urlsplit(url).path not in ("", "/"):
                self.index.setdefault(url, {})["failed"] = e.get("failed", 0) + 1
                self.dirty = True
            return None
        self.cache[url] = data
        if ARCHIVE_IMAGES and self.total + len(data) <= MAX_ARCHIVE_MB * 1_000_000:
            fname = hashlib.sha1(url.encode()).hexdigest()[:16] + sniff(data)[1]
            IMAGE_DIR.mkdir(exist_ok=True)
            (IMAGE_DIR / fname).write_bytes(data)
            self.index[url] = {"file": fname, "bytes": len(data)}
            self.total += len(data)
            self.new_saved += 1
            self.dirty = True
        return data

    def archive_many(self, urls, limit):
        done = 0
        for u in urls:
            e = self.index.get(u) or {}
            if u in self.cache or e.get("file") or e.get("failed", 0) >= 3:
                continue
            if done >= limit:
                break
            self.get(u)
            done += 1

    def save(self):
        if ARCHIVE_IMAGES and self.dirty:
            IMAGE_DIR.mkdir(exist_ok=True)
            (IMAGE_DIR / "index.json").write_text(json.dumps(self.index, indent=1, sort_keys=True))

    def summary(self):
        if not ARCHIVE_IMAGES:
            return "image archive off"
        saved = sum(1 for e in self.index.values() if e.get("file"))
        dead = sum(1 for e in self.index.values() if not e.get("file") and e.get("failed", 0) >= 3)
        return (f"images: {self.new_saved} saved this run, {saved} in archive ({self.total / 1e6:.1f} MB)"
                + (f", {dead} unreachable" if dead else ""))

    def thumbs_for(self, urls, budget=1_600_000):
        """{url: small image bytes} for the archived images among `urls` (disk only, never downloads)."""
        out, used = {}, 0
        for u in urls:
            e = self.index.get(u) or {}
            if not e.get("file"):
                continue
            try:
                t = make_thumb((IMAGE_DIR / e["file"]).read_bytes())
            except OSError:
                continue
            if t and used + len(t) <= budget:
                out[u] = t
                used += len(t)
        return out

    def images_for(self, old_text, new_text):
        """Bytes for the images that were added or removed between two snapshots."""
        _, oi, _ = parse_snapshot(old_text)
        _, ni, _ = parse_snapshot(new_text)
        changed = [u for u in ni if u not in set(oi)][:12] + [u for u in oi if u not in set(ni)][:12]
        out = {}
        for u in changed:
            data = self.get(u)
            if data:
                out[u] = data
        return out


def save_raw(name, raw):
    """Keep the page HTML so the next change can show an exact 'before' copy."""
    if not raw or len(raw) > MAX_RAW:
        return
    path = SNAP_DIR / f"{name}.html"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == raw:
            return
        SNAP_DIR.mkdir(exist_ok=True)
        path.write_text(raw, encoding="utf-8")
    except OSError as e:
        print(f"[{name}] could not save page copy: {e}", file=sys.stderr)


def load_raw(name):
    try:
        return (SNAP_DIR / f"{name}.html").read_text(encoding="utf-8")
    except OSError:
        return None


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
    for name, cfg in SECTION_WATCH.items():
        yield name, cfg["url"], (lambda u, c=cfg: section_snapshot(u, c)), ""


def main():
    state = load_state()
    known = set(WATCH) | set(KEYWORD_WATCH) | set(SECTION_WATCH)
    for gone in [k for k in state if k not in known]:  # removed from the lists: forget it completely
        del state[gone]
        try:
            (SNAP_DIR / f"{gone}.html").unlink()
        except OSError:
            pass
    stamp = datetime.now(LOCAL_TZ).strftime("%d %b %H:%M") + " AWST"
    alerts, baselined, notes, pending = [], [], [], []
    arch, current_urls, wb_jobs = Archive(), [], []  # wb_jobs: (name, url, alert or None, previous capture)
    for name, url, snap, note in targets():
        new = snap(url)
        raw = new.pop("_raw", None)
        status = new["status"]
        old = state.get(name)

        if status in ("http 404", "http 410"):  # page doesn't exist (yet)
            if old and old.get("status") == "ok":
                old["miss"] = old.get("miss", 0) + 1  # needs 2 misses in a row, avoids blips
                if old["miss"] >= 2:
                    alerts.append(gone_alert(name, url, stamp, old.get("text", ""), status[5:], note,
                                             raw=load_raw(name)))
                    state[name] = {"status": "missing", "text": old.get("text", ""), "wayback": old.get("wayback")}
            elif not old:
                state[name] = {"status": "missing"}
                notes.append(f"{name}: not live yet (404), will alert when it appears")
            continue
        if status != "ok":
            print(f"[{name}] {status} (skipped)", file=sys.stderr)
            continue
        current_urls += parse_snapshot(new["text"])[1]

        if old and old.get("status") == "missing":  # it just appeared
            if old.get("text"):
                before_raw = load_raw(name)
                alert = change_alert(name, url, stamp, old["text"], new["text"], "is back", note,
                                     images=arch.images_for(old["text"], new["text"]),
                                     raw_before=before_raw, raw_after=raw,
                                     thumbs=arch.thumbs_for(list(dict.fromkeys(image_urls(before_raw or "") + image_urls(raw or "")))))
            else:
                alert = live_alert(name, url, stamp, new["text"], note, raw=raw)
            alerts.append(alert)
            wb_jobs.append((name, url, alert, old.get("wayback")))
            state[name] = new
            save_raw(name, raw)
            continue
        if old:
            old.pop("miss", None)
        if not old or old.get("status") != "ok" or old.get("v") != SNAP_V:
            state[name] = new
            save_raw(name, raw)
            wb_jobs.append((name, url, None, None))
            n = len(new["text"].splitlines()) if new.get("mode") == "keywords" else None
            baselined.append(name if n is None else f"{name} ({n} GTA matches)")
            continue
        if new["hash"] == old["hash"]:
            old.pop("pending", None)  # change flipped back, ignore it
            if not (SNAP_DIR / f"{name}.html").exists():
                save_raw(name, raw)  # first run after upgrading: save the copy we will compare against later
            if not old.get("wayback") and old.get("wb_fail", 0) < 4:
                wb_jobs.append((name, url, None, None))  # never captured yet (or earlier tries failed)
            continue
        if CONFIRM and old.get("pending") != new["hash"]:
            old["pending"] = new["hash"]  # first sighting, wait for the next run
            pending.append(name)
            continue
        before_raw = load_raw(name)
        alert = change_alert(name, url, stamp, old["text"], new["text"], "changed", note,
                             images=arch.images_for(old["text"], new["text"]),
                             raw_before=before_raw, raw_after=raw,
                             thumbs=arch.thumbs_for(list(dict.fromkeys(image_urls(before_raw or "") + image_urls(raw or "")))))
        alerts.append(alert)
        wb_jobs.append((name, url, alert, old.get("wayback")))
        state[name] = new
        save_raw(name, raw)

    wb_note = ""
    wb_jobs = [j for j in wb_jobs if j[0] in WAYBACK_PAGES]
    if ARCHIVE_WAYBACK and wb_jobs:
        jobs = sorted(wb_jobs, key=lambda j: j[2] is None)[:WAYBACK_MAX_PER_RUN]  # pages with alerts first
        results = wayback_many([j[1] for j in jobs], limit=WAYBACK_MAX_PER_RUN)
        saved = 0
        for name, url, alert, before in jobs:
            link, why = results.get(url, (None, "skipped"))
            entry = state.get(name) or {}
            if link:
                saved += 1
                entry["wayback"] = link
                entry.pop("wb_fail", None)
            elif entry.get("status") == "ok":
                entry["wb_fail"] = entry.get("wb_fail", 0) + 1
            if alert is not None:
                alert["text"] += "\n" + wayback_line(before, link, why)
        wb_note = f"wayback: {saved}/{len(jobs)} saved"
    arch.archive_many(list(dict.fromkeys(current_urls)), MAX_DOWNLOADS_PER_RUN)
    arch.save()
    STATE_FILE.write_text(json.dumps(state, indent=2))
    if alerts:
        send_alerts(alerts, PING_TEXT)
        print(f"[{stamp}] {wb_note + '; ' if wb_note else ''}{arch.summary()}")
        return
    msgs = []
    if baselined:
        msgs.append(f"baseline saved for: {', '.join(baselined)}")
    msgs += notes
    if wb_note:
        msgs.append(wb_note)
    if pending:
        msgs.append(f"unconfirmed change on: {', '.join(pending)}")
    print(f"[{stamp}] " + "; ".join((msgs or ["no changes"]) + [arch.summary()]))


if __name__ == "__main__":
    main()
