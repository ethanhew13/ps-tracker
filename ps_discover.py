#!/usr/bin/env python3
"""
Discovery watcher: lots of sources, very little spam.

  * New item that mentions GTA / Rockstar / Vice City / Take-Two etc.  -> flagged alert right away (@everyone)
  * New item that could be related but has none of those words         -> goes into ONE daily digest at 12:00 UTC
  * Everything else                                                    -> ignored
  * Certificate transparency + merch store changes                     -> see their own notes below

Source kinds (see SOURCES at the bottom of the config):
  feed     RSS / Atom feed (tries candidate URLs, then auto-discovers one from "page")
  links    list page: every new link on the page is an item (venue calendars, news pages)
  lines    list page: every new visible text line matching "line_filter" is an item (FAA TFR list)
  pdf      page that links a PDF (lobbyist lists): every new line in the PDF is an item (needs pypdf)
  catalog  Xsolla store catalog: new / removed items and price changes, plus merch/merch_catalog.csv
  ct       certificate transparency: new hostnames on the listed domains (certspotter, crt.sh as backup)

Every source gets a silent baseline on its first run. Needs ps_common.py and ps_feed_watch.py next to it.
Stdlib only, except pypdf for the PDF sources (pip install pypdf; without it those sources only notice a new PDF).
"""
import csv
import hashlib
import io
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from ps_common import cut, send_alerts, to_lines
from ps_feed_watch import _text, fetch, is_old, load_posts, parse_date, since_cutoff

STATE_FILE = Path(__file__).with_name("ps_discover_state.json")
MERCH_DIR = Path(__file__).with_name("merch")

PING_TEXT = "@everyone"        # flagged alerts ping; the daily digest never does
DIGEST_HOUR_UTC = 12           # the digest goes out on the first run at/after this hour (UTC)
MAX_FLAG_MESSAGES = 4          # per run, extra flagged items fall into the digest instead
MAX_ITEMS_PER_FLAG = 5         # items listed per flagged message
MAX_ITEMS_PER_DIGEST_SOURCE = 12
FLOOD_LIMIT = 40               # this many new items on a list page at once = layout change, re-baseline quietly
SEEN_CAP = 2500

# --- what counts as "directly about it" -> flagged immediately
DIRECT = re.compile(
    r"\bgta\b|gta[\s_-]*(?:vi|6)\b|gtavi|grand[\s_-]*theft[\s_-]*auto|vice[\s_-]*city|leonida|rockstar"
    r"|take[\s-]*two|take2", re.I)
# digest lines with these get a "!" so odd-looking bookings stand out
SUSPECT = re.compile(
    r"\b(private event|tba|tbd|to be announced|venue hold|buyout|corporate event|brand activation|activation"
    r"|pop-?up|special event|closed to the public)\b", re.I)
# "could be related" words for sources that publish a lot (news, trade press)
RELATED_NEWS = re.compile(
    r"miami|kaseya|billboard|out[\s-]?of[\s-]?home|\booh\b|takeover|pop-?up|activation|experiential|permit"
    r"|launch event|midnight launch|mural|rooftop|helicopter|drone show", re.I)


def gnews(query):
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"


YEAR = datetime.now(timezone.utc).year

SOURCES = [
    # ---------------------------------------------------------------- news
    {"name": "gnews-gta6-miami", "label": "Google News: GTA VI + Miami", "kind": "feed", "interval": 30,
     "urls": [gnews('"Grand Theft Auto VI" (Miami OR "Vice City" OR "Kaseya Center") '
                    '(permit OR billboard OR event OR takeover OR sign OR "pop-up" OR activation)')],
     "digest": "related", "related": RELATED_NEWS},
    {"name": "gnews-rockstar-miami", "label": "Google News: Rockstar + Miami", "kind": "feed", "interval": 30,
     "urls": [gnews('Rockstar Games Miami (permit OR billboard OR event OR sign OR "Kaseya Center" OR rooftop)')],
     "digest": "related", "related": RELATED_NEWS},
    {"name": "gnews-gta6-ooh", "label": "Google News: GTA 6 outdoor / launch events", "kind": "feed", "interval": 60,
     "urls": [gnews('"GTA 6" (billboard OR "out of home" OR takeover OR "launch event" OR "midnight launch")')],
     "digest": "related", "related": RELATED_NEWS},
    # ---------------------------------------------------------------- ad trade press (digest only if OOH-ish)
    {"name": "adweek", "label": "Adweek", "kind": "feed", "interval": 60, "digest": "related", "related": RELATED_NEWS,
     "urls": ["https://www.adweek.com/feed/"], "page": "https://www.adweek.com/"},
    {"name": "adage", "label": "Ad Age", "kind": "feed", "interval": 60, "digest": "related", "related": RELATED_NEWS,
     "urls": ["https://adage.com/rss.xml"], "page": "https://adage.com/"},
    {"name": "thedrum", "label": "The Drum", "kind": "feed", "interval": 60, "digest": "related", "related": RELATED_NEWS,
     "urls": [], "page": "https://www.thedrum.com/"},
    {"name": "marketingdive", "label": "Marketing Dive", "kind": "feed", "interval": 60, "digest": "related",
     "related": RELATED_NEWS, "urls": ["https://www.marketingdive.com/feeds/news/"], "page": "https://www.marketingdive.com/"},
    # ---------------------------------------------------------------- contractors and ad vendors (everything new -> digest)
    {"name": "dimensional-innovations", "label": "Dimensional Innovations", "kind": "feed", "interval": 120,
     "digest": "all", "urls": ["https://www.di.net/feed/"], "page": "https://www.di.net/",
     "fallback_links": {"include": r"/(project|news|work|case)"}},
    # ---------------------------------------------------------------- Miami venue calendars
    {"name": "venue-kaseya", "label": "Kaseya Center events", "kind": "links", "interval": 120, "digest": "all",
     "url": "https://www.kaseyacenter.com/events/", "include": r"/events/detail/"},
    {"name": "venue-hardrock", "label": "Hard Rock Stadium events", "kind": "links", "interval": 120, "digest": "all",
     "url": "https://www.hardrockstadium.com/events/", "include": r"/event"},
    {"name": "venue-bayfront", "label": "Bayfront Park events", "kind": "links", "interval": 120, "digest": "all",
     "url": "https://www.bayfrontparkmiami.com/events/", "include": r"/event"},
    {"name": "venue-mbcc", "label": "Miami Beach Convention Center events", "kind": "links", "interval": 120,
     "digest": "all", "url": "https://www.miamibeachconvention.com/events", "include": r"/event"},
    # ---------------------------------------------------------------- permits and airspace
    {"name": "faa-tfr", "label": "FAA flight restrictions (Florida)", "kind": "lines", "interval": 60, "digest": "all",
     "url": "https://tfr.faa.gov/tfr2/list.html", "line_filter": r"\bFL\b|Florida|Miami"},
    # ---------------------------------------------------------------- lobbyist registries (PDF lists)
    {"name": "lobby-miami-beach", "label": "Miami Beach lobbyist list", "kind": "pdf", "interval": 180, "digest": "all",
     "page": "https://www.miamibeachfl.gov/city-hall/city-clerk/lobbyist-information/",
     "link_text": r"active\s*issues"},
    {"name": "lobby-miami", "label": "City of Miami lobbyist list", "kind": "pdf", "interval": 180, "digest": "all",
     "page": "https://archive.miami.gov/City_Clerk/pages/lobbyist/lobbyist.asp", "follow": rf"^\s*{YEAR}\s*$",
     "link_text": r"active\s*registration"},
    # ---------------------------------------------------------------- merch store (Xsolla project 100851)
    {"name": "merch-store", "label": "GTA VI merch store", "kind": "catalog", "interval": 30,
     "endpoints": ["https://store.xsolla.com/api/v2/project/100851/items/virtual_items",
                   "https://store.xsolla.com/api/v2/project/100851/items"]},
    # ---------------------------------------------------------------- certificate transparency (daily summary only)
    {"name": "cert-transparency", "label": "Certificate transparency", "kind": "ct", "interval": 60,
     "domains": ["rockstargames.com", "take2games.com", "rockstarwarehouse.com"]},
]

# link texts that say nothing about the event / item they point at
GENERIC_LINK = re.compile(
    r"^(buy tickets?|tickets?|get tickets?|learn more|more info(rmation)?|read more|view (all|more|details)|details"
    r"|see more|register|rsvp|book now|click here|find out more|shop now)$", re.I)
A_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*(?:"([^"]*)"|'([^']*)')[^>]*>(.*?)</a>""", re.I | re.S)
PDF_BOILER = re.compile(
    r"^(registered lobbyists and active issues|principal name|issue description|registration|page \d+.*|\d+)$", re.I)


def h(s):
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def anchors(page, base):
    for m in A_RE.finditer(page):
        href = (m.group(1) or m.group(2) or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urldefrag(urljoin(base, re.sub(r"&amp;", "&", href)))[0]
        if full.startswith("http"):
            yield _text(m.group(3)), full


def item(i_id, title, url="", snippet="", text="", date=""):
    return {"id": i_id, "title": title, "url": url, "snippet": snippet, "text": text or f"{title} {snippet}", "date": date}


# ---------------------------------------------------------------- source runners: (items, error, silent_ids)
def run_feed(src, st):
    posts, used = load_posts({"urls": src.get("urls", []), "page": src.get("page")}, st.get("feed_url"))
    if posts is not None:
        st["feed_url"] = used
        return [item(p["id"], p["title"], p["link"], p["desc"], f"{p['title']} {p['desc']} {_text(p['body'])[:3000]}",
                     p["pub"]) for p in posts], None, []
    if src.get("fallback_links"):  # no feed anywhere: treat the page as a list of links
        return run_links({"url": src["page"], **src["fallback_links"], "min_items": 3}, st)
    return [], "no working feed found", []


def run_links(src, st):
    status, data = fetch(src["url"])
    if status != "ok":
        return [], status, []
    inc = re.compile(src["include"], re.I) if src.get("include") else None
    base_host = urlsplit(src["url"]).netloc.lower().removeprefix("www.")
    items, seen = [], set()
    for text, full in anchors(data.decode("utf-8", "replace"), src["url"]):
        host = urlsplit(full).netloc.lower().removeprefix("www.")
        if ((inc and not inc.search(full)) or len(text) < src.get("min_text", 6) or full in seen
                or GENERIC_LINK.match(text) or (src.get("same_domain", True) and not host.endswith(base_host))):
            continue
        seen.add(full)
        items.append(item(full, text, full))
    if len(items) < src.get("min_items", 3):
        return [], "too few links (JavaScript page or blocked?)", []
    return items, None, []


def run_lines(src, st):
    status, data = fetch(src["url"])
    if status != "ok":
        return [], status, []
    lines = to_lines(data.decode("utf-8", "replace"))
    if len(lines) < 10:
        return [], "too little text (JavaScript page or blocked?)", []
    rx = re.compile(src["line_filter"], re.I)
    return [item(l, l, src["url"]) for l in lines if rx.search(l)], None, []


def pdf_lines(data):
    """Text lines of a PDF, or None when pypdf isn't installed."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    out = []
    for pg in PdfReader(io.BytesIO(data)).pages:
        out += (pg.extract_text() or "").splitlines()
    return out


def _pdf_date(s):
    m = re.search(r"(\d{1,2})[-_/](\d{1,2})[-_/](20\d{2})", s)
    if m:
        return (int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.search(r"(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})", s)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 0)


def run_pdf(src, st):
    status, data = fetch(src["page"])
    if status != "ok":
        return [], status, []
    page = data.decode("utf-8", "replace")
    want = re.compile(src["link_text"], re.I)

    def pdf_links(html_page, base):
        return [(t, u) for t, u in anchors(html_page, base) if u.lower().split("?")[0].endswith(".pdf") and want.search(f"{t} {u}")]
    links = pdf_links(page, src["page"])
    if not links and src.get("follow"):  # e.g. a "2026" link that leads to the year's PDFs
        follow = re.compile(src["follow"], re.I)
        for t, u in anchors(page, src["page"]):
            if follow.search(t):
                s2, d2 = fetch(u)
                if s2 == "ok":
                    links = pdf_links(d2.decode("utf-8", "replace"), u)
                    break
    if not links:
        return [], "no matching PDF link found", []
    text, link = max(links, key=lambda tu: (_pdf_date(f"{tu[0]} {tu[1]}"), tu[1]))  # newest dated PDF, stable choice
    status, pdf = fetch(link)
    if status != "ok":
        return [], f"PDF {status}", []
    lines = pdf_lines(pdf)
    if lines is None:  # no pypdf: a new dated PDF link is the only signal we can give
        return [item(link, text or link, link)], None, []
    out = []
    for l in lines:
        l = re.sub(r"\s+", " ", l).strip()
        if len(l) >= 10 and not PDF_BOILER.match(l):
            out.append(item(l, l, link))
    if not out:
        return [], "PDF had no readable text", []
    return out, None, []


def write_merch_csv(items, prev_rows, today):
    MERCH_DIR.mkdir(exist_ok=True)
    rows = dict(prev_rows)
    for sku, it in items.items():
        old = rows.get(sku, {})
        rows[sku] = {"sku": sku, "name": it["name"], "price": it["price"], "type": it["type"], "image_url": it["image"],
                     "first_seen": old.get("first_seen") or today, "last_seen": today}
    with open(MERCH_DIR / "merch_catalog.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "name", "price", "type", "image_url", "first_seen", "last_seen"])
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["first_seen"], r["sku"])))


def read_merch_csv():
    try:
        with open(MERCH_DIR / "merch_catalog.csv", newline="", encoding="utf-8") as f:
            return {r["sku"]: r for r in csv.DictReader(f)}
    except OSError:
        return {}


def run_catalog(src, st):
    items, ok_all = {}, True
    for ep in src["endpoints"]:
        off, ok_all = 0, True
        for _ in range(20):
            status, data = fetch(f"{ep}{'&' if '?' in ep else '?'}limit=50&offset={off}&locale=en")
            if status != "ok":
                ok_all = False
                break
            try:
                d = json.loads(data)
            except ValueError:
                ok_all = False
                break
            batch = d.get("items") or []
            for it in batch:
                sku = str(it.get("sku") or it.get("item_id"))
                pr = it.get("price") or {}
                items[sku] = {"name": it.get("name") or sku, "type": it.get("type") or "", "image": it.get("image_url") or "",
                              "price": f"{pr.get('amount', '')} {pr.get('currency', '')}".strip()}
            if not d.get("has_more") or not batch:
                break
            off += 50
        if items and ok_all:
            break
    if not items or not ok_all:
        return [], "catalog request failed or came back empty", []
    prev = st.get("catalog")
    st["catalog"] = {k: [v["name"], v["price"]] for k, v in items.items()}
    today = datetime.now(timezone.utc).date().isoformat()
    if prev is None:  # first run: baseline, no alert
        write_merch_csv(items, {}, today)
        return [], None, []
    new = [k for k in items if k not in prev]
    gone = [k for k in prev if k not in items]
    priced = [k for k in items if k in prev and prev[k][1] != items[k]["price"]]
    if new or gone or priced:
        write_merch_csv({k: items[k] for k in new + priced}, read_merch_csv(), today)
        lines = [f"**{src['label']}**: {len(new)} new, {len(priced)} price changes, {len(gone)} removed"]
        lines += [f"+ {items[k]['name']} ({items[k]['price'] or 'no price'}) {items[k]['image']}".rstrip() for k in new[:8]]
        lines += [f"~ {items[k]['name']}: {prev[k][1] or '?'} -> {items[k]['price'] or '?'}" for k in priced[:8]]
        lines += [f"- {prev[k][0]} (removed)" for k in gone[:8]]
        if len(new) + len(priced) + len(gone) > 24:
            lines.append("... more in merch/merch_catalog.csv")
        st["_alert"] = "\n".join(lines)
    return [], None, []


def ct_certspotter(domain, st, budget=12):
    after = st.setdefault("ct_after", {}).get(domain)
    names, done = set(), False
    for _ in range(budget):
        url = (f"https://api.certspotter.com/v1/issuances?domain={domain}&include_subdomains=true&expand=dns_names"
               + (f"&after={after}" if after else ""))
        status, data = fetch(url)
        if status != "ok":
            return names, after, False, status
        try:
            rows = json.loads(data)
        except ValueError:
            return names, after, False, "bad json"
        if not rows:
            done = True
            break
        for r in rows:
            names.update(n.lower().lstrip("*.") for n in r.get("dns_names", []))
            after = r.get("id", after)
    return names, after, done, None


def run_ct(src, st):
    items, silent, errs = [], [], []
    done_map = st.setdefault("ct_done", {})
    for d in src["domains"]:
        names, after, done, err = ct_certspotter(d, st)
        if err:  # rate limited or down: crt.sh as the backup (returns every currently valid name)
            status, data = fetch(f"https://crt.sh/?q=%25.{d}&exclude=expired&output=json")
            try:
                rows = json.loads(data) if status == "ok" else None
            except ValueError:
                rows = None
            if rows is None:
                errs.append(f"{d}: {err}")
                st["ct_after"][d] = after
                silent += [n for n in names if n.endswith(d)]  # keep partial baseline progress
                continue
            names = {n.lower().lstrip("*.") for r in rows for n in r.get("name_value", "").split("\n")}
            done = True
        st["ct_after"][d] = after
        names = {n for n in names if n.endswith(d)}
        if done_map.get(d):
            items += [item(n, n, f"https://crt.sh/?q={n}") for n in sorted(names)]
        else:
            silent += sorted(names)
            if done:
                done_map[d] = True
    if errs and not items and not silent:
        return [], "; ".join(errs), []
    return items, None, silent


RUNNERS = {"feed": run_feed, "links": run_links, "lines": run_lines, "pdf": run_pdf, "catalog": run_catalog, "ct": run_ct}


def safe_run(src, st):
    try:
        return RUNNERS[src["kind"]](src, st)
    except Exception as e:  # one broken source must never break the rest
        return [], f"{type(e).__name__}: {str(e)[:80]}", []


# ---------------------------------------------------------------- classify, alert, digest
def classify(it, src):
    m = DIRECT.search(f"{it['text']} {it['url']}")
    if m or src["kind"] == "ct":
        return "flag", (m.group(0).lower() if m else "")
    if src.get("digest") == "all":
        return "digest", ""
    if src.get("digest") == "related" and src["related"].search(it["text"]):
        return "digest", ""
    return "drop", ""


def flag_message(src, batch, extra):
    tag = f"[GTA match: {batch[0][1]}] " if batch[0][1] else ""
    out = [f"{tag}**{src['label']}** ({len(batch) + extra} new)"]
    norm = lambda t: re.sub(r"\W+", " ", t.lower()).strip()
    for it, _ in batch:
        when = parse_date(it.get("date"))
        out.append(f"- {cut(it['title'], 140)}" + (f" ({when:%d %b %H:%M} UTC)" if when else ""))
        if it["url"]:
            out.append(f"  {it['url']}")
        sn = it["snippet"]
        if sn and not norm(sn).startswith(norm(it["title"])[:40]) and not norm(it["title"]).startswith(norm(sn)[:40]):
            out.append(f"  {cut(sn, 160)}")  # skip snippets that just repeat the headline
    if extra:
        out.append(f"... and {extra} more (they're in the next daily digest)")
    return "\n".join(out)


def build_digest(queue, sources_state, now, first):
    by, ct = {}, []
    for q in queue:
        (ct if q.get("ct") else by.setdefault(q["label"], [])).append(q)
    out = [f"**Daily digest** ({now:%d %b}, {DIGEST_HOUR_UTC}:00 UTC)"]
    if by:
        out.append("New things that could be related (no GTA / Rockstar / Take-Two words in them):")
    for label, qs in by.items():
        out += ["", f"**{label}** ({len(qs)})"]
        for q in qs[:MAX_ITEMS_PER_DIGEST_SOURCE]:
            out.append(f"- {'! ' if q.get('mark') else ''}{cut(q['title'], 100)}" + (f" <{q['url']}>" if q.get("url") else ""))
        if len(qs) > MAX_ITEMS_PER_DIGEST_SOURCE:
            out.append(f"  ... and {len(qs) - MAX_ITEMS_PER_DIGEST_SOURCE} more")
    if ct:
        out += ["", f"**Certificate transparency, last 24h** ({len(ct)} new hostnames)"]
        out += [f"- {q['title']}" + (" (flagged earlier)" if q.get("flagged") else "") for q in ct[:40]]
        if len(ct) > 40:
            out.append(f"  ... and {len(ct) - 40} more")
    if not by and not ct:
        out.append("Nothing new to report.")
    bad = [f"{n} ({s['status']})" for n, s in sources_state.items()
           if s.get("fails", 0) >= (1 if first else 3) and not s.get("bad_reported")]
    if first:
        working = sum(1 for s in sources_state.values() if s.get("baselined") and not s.get("fails"))
        out += ["", f"Source check: {working}/{len(sources_state)} sources working so far."]
    if bad:
        out += ["Not working" + ("" if first else " (3+ failed runs in a row)") + ", checked once a day from now on: " + ", ".join(bad)]
    out += ["! = could be a private / unannounced booking"] if any(q.get("mark") for q in queue) else []
    return "\n".join(out)


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def main(now=None):
    now = now or datetime.now(timezone.utc)
    state = load_state()
    cutoff = since_cutoff(state, now)
    sstate = state.setdefault("sources", {})
    queue = state.setdefault("queue", [])
    def is_due(s):
        st = sstate.setdefault(s["name"], {})
        minutes = max(s.get("interval", 60), 1440) if st.get("fails", 0) >= 6 else s.get("interval", 60)  # back off
        return now.timestamp() - st.get("last_run", 0) >= minutes * 60 - 90
    due = [s for s in SOURCES if is_due(s)]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda s: safe_run(s, sstate[s["name"]]), due))

    outbox = []  # (source or None, message, flagged items)
    for src, (items, err, silent) in zip(due, results):
        st = sstate[src["name"]]
        st["last_run"] = now.timestamp()
        if err:
            st["fails"] = st.get("fails", 0) + 1
            st["status"] = err
            print(f"[{src['name']}] problem: {err}", file=sys.stderr)
            continue
        st["fails"], st["status"], st["bad_reported"] = 0, "ok", False
        if src["kind"] == "catalog":
            if st.get("_alert"):
                outbox.append((None, {"text": st.pop("_alert"), "files": []}, []))
            st["baselined"] = True
            print(f"[{src['name']}] ok: {len(st.get('catalog', {}))} items")
            continue

        key = "pdf_lines" if src["kind"] == "pdf" else "seen"  # a PDF is compared with its previous version only
        if src["kind"] == "pdf" and key not in st:
            st["baselined"] = False  # state saved by an older version: start over silently, don't replay the whole PDF
        seen = st.get(key, [])
        seen_set = set(seen)
        fresh = [i for i in items if h(i["id"]) not in seen_set]
        stale = [i for i in fresh if is_old(i, cutoff)]  # published before the cutoff: remember, never report
        fresh = [i for i in fresh if not is_old(i, cutoff)]
        seen += [h(i["id"]) for i in stale]
        seen_set.update(h(i["id"]) for i in stale)
        for i in silent:  # progress we learn silently (certificate history, etc.)
            if h(i) not in seen_set:
                seen.append(h(i))
                seen_set.add(h(i))
        if not st.get("baselined"):
            seen += [h(i["id"]) for i in fresh]
            st["baselined"] = True
            print(f"[{src['name']}] baseline: {len(items)} items")
        else:
            flood = src["kind"] in ("links", "lines", "pdf") and len(fresh) > FLOOD_LIMIT
            if flood:  # layout change: don't flood the digest, but still catch any keyword hits
                queue.append({"label": src["label"], "url": src.get("url") or src.get("page") or "",
                              "title": f"{len(fresh)} new items at once, looks like a layout change, re-baselined quietly"})
            seen += [h(i["id"]) for i in fresh]
            flags = []
            for it in fresh:
                kind, why = classify(it, src)
                if kind == "flag":
                    flags.append((it, why))
                if src["kind"] == "ct":
                    queue.append({"label": src["label"], "title": it["title"], "ct": True, "flagged": kind == "flag"})
                elif kind == "digest" and not flood:
                    queue.append({"label": src["label"], "title": it["title"], "url": it["url"],
                                  "mark": bool(SUSPECT.search(it["title"]))})
            if flags:
                batch, extra = flags[:MAX_ITEMS_PER_FLAG], len(flags) - MAX_ITEMS_PER_FLAG
                outbox.append((src, {"text": flag_message(src, batch, max(0, extra)), "files": []}, flags))
            print(f"[{src['name']}] ok: {len(items)} items, {len(fresh)} new, {len(flags)} flagged")
        st[key] = list(dict.fromkeys(h(i["id"]) for i in items)) if src["kind"] == "pdf" else seen[-SEEN_CAP:]

    # flagged messages are capped per run; the rest of the flagged items are not lost, they go to the digest
    to_send, spill = [], 0
    for src, msg, flags in outbox:
        if src is None or len(to_send) < MAX_FLAG_MESSAGES:
            to_send.append(msg)
        else:
            for it, _ in flags:
                queue.append({"label": src["label"], "title": f"[flagged] {it['title']}", "url": it["url"], "flagged": True})
            spill += 1

    today = now.date().isoformat()
    digest = None
    if now.hour >= DIGEST_HOUR_UTC and state.get("digest_date") != today:
        first = not state.get("digest_date")
        newly_bad = [n for n, st in sstate.items() if st.get("fails", 0) >= (1 if first else 3) and not st.get("bad_reported")]
        if queue or newly_bad or first:  # quiet days send nothing
            digest = {"text": build_digest(queue, sstate, now, first=first), "files": []}
        for s in sstate.values():  # a failing source is reported once, not every day
            if s.get("fails", 0) >= (1 if first else 3):
                s["bad_reported"] = True
        state["digest_date"] = today
        queue.clear()
    state["queue"] = queue[-1500:]

    STATE_FILE.write_text(json.dumps(state, indent=1))
    if to_send:
        send_alerts(to_send, PING_TEXT)
    if digest:
        send_alerts([digest], "")
    if not to_send and not digest:
        print(f"[{now:%d %b %H:%M} UTC] nothing to report ({len(due)} sources checked)")


if __name__ == "__main__":
    main()
