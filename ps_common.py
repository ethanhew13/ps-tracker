#!/usr/bin/env python3
"""
Shared helpers for the PlayStation watchers (ps_page_watch.py, ps_feed_watch.py):
page parsing, change diffing, readable Discord summaries and before/after HTML previews.
Stdlib only. Keep this file in the same folder as the watcher scripts.
"""
import base64
import difflib
import html
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PRICE = re.compile(r"\$\d[\d,]*\.\d\d")
TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
KEY_RE = re.compile(r"""(?:name|property)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
VAL_RE = re.compile(r"""content\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)
BLOCK_RE = re.compile(
    r"</?(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|section|article|header|footer|nav|button|label|option)\b[^>]*>",
    re.I)
IMG_RE = re.compile(
    r"""https?://[^\s"'<>()\\]+?\.(?:png|jpe?g|webp|gif|avif|svg)\b"""
    r"""|https?://(?:image\.api|gmedia)\.playstation\.com[^\s"'<>()\\]*""", re.I)
LINK_LINE = re.compile(r"^\[link\] (.*) -> (\S+)$")
esc = html.escape

MAX_ATTACH = 4            # changed images attached to a Discord alert, per side (new / removed)
ATTACH_BYTES = 5_000_000  # total size of attached images per alert
EMBED_ONE = 600_000       # biggest image embedded straight into the HTML previews
EMBED_TOTAL = 1_500_000   # total size of images embedded into the HTML previews
UPLOAD_LIMIT = 7_500_000  # stay safely under Discord's upload limit


def sniff(data):
    """(mime type, extension) from the first bytes of an image, or ('application/octet-stream', '.bin')."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp" and b"avif" in data[8:16]:
        return "image/avif", ".avif"
    if b"<svg" in data[:500].lower():
        return "image/svg+xml", ".svg"
    return "application/octet-stream", ".bin"


# ---------------------------------------------------------------- page parsing
def parse_meta(page):
    out = {}
    for tag in TAG_RE.findall(page):
        k, v = KEY_RE.search(tag), VAL_RE.search(tag)
        if k and v:
            out[(k.group(1) or k.group(2)).lower()] = html.unescape(v.group(1) or v.group(2) or "")
    return out


def to_lines(page, ignore=()):
    """Visible text of an HTML page as a list of clean lines, in reading order."""
    s = re.sub(r"<(script|style|noscript|svg|template)\b.*?</\1>", " ", page, flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    s = BLOCK_RE.sub("\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    lines = []
    for raw in html.unescape(s).splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or any(p.search(line) for p in ignore):
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


def parse_snapshot(text):
    """Split a saved snapshot into (meta dict, image urls, text lines)."""
    meta, imgs, lines = {}, [], []
    for l in (text or "").splitlines():
        if l.startswith("[meta] "):
            k, _, v = l[7:].partition(": ")
            meta[k] = v
        elif l.startswith("[img] "):
            imgs.append(l[6:])
        else:
            lines.append(l)
    return meta, imgs, lines


# ---------------------------------------------------------------- text helpers
def cut(s, n=130):
    """Shorten at a word boundary with an ellipsis."""
    s = " ".join(s.split())
    if len(s) <= n:
        return s
    c = s[:n].rsplit(" ", 1)[0] or s[:n]
    return c.rstrip(",.;:-") + "…"


def snippet_pair(a, b, ctx=5, full=130):
    """Short versions of two similar lines, focused on the part that differs."""
    if len(a) <= full and len(b) <= full:
        return a, b
    ta, tb = a.split(), b.split()
    p = 0
    while p < min(len(ta), len(tb)) and ta[p] == tb[p]:
        p += 1
    s = 0
    while s < min(len(ta), len(tb)) - p and ta[-1 - s] == tb[-1 - s]:
        s += 1

    def part(t):
        start, end = max(0, p - ctx), min(len(t), len(t) - s + ctx)
        return ("…" if start > 0 else "") + cut(" ".join(t[start:end]), 200) + ("…" if end < len(t) else "")
    return part(ta), part(tb)


def short_img(u):
    sp = urlsplit(u)
    parts = [x for x in sp.path.split("/") if x]
    name = parts[-1] if parts else sp.netloc
    stem, dot, ext = name.rpartition(".")
    if dot and len(ext) <= 4:
        if len(stem) > 14:
            name = stem[:12] + "…" + dot + ext
    elif len(name) > 34:
        name = name[:14] + "…" + name[-14:]
    prev = parts[-2] if len(parts) > 1 else ""
    return f".../{prev}/{name}" if prev else name


def word_diff_html(a, b):
    """HTML for two similar strings with the differing words wrapped in <del>/<ins>."""
    ta, tb = a.split(), b.split()
    ha, hb = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        x, y = " ".join(ta[i1:i2]), " ".join(tb[j1:j2])
        if tag == "equal":
            ha.append(esc(x))
            hb.append(esc(y))
        else:
            if x:
                ha.append(f"<del>{esc(x)}</del>")
            if y:
                hb.append(f"<ins>{esc(y)}</ins>")
    return " ".join(ha), " ".join(hb)


# ---------------------------------------------------------------- diffing
def _pair_lines(olds, news):
    """Match edited lines (old index -> new index) by word similarity."""
    pairs, used = {}, set()
    if len(olds) * len(news) > 1600:
        if len(olds) == len(news):  # big block of same size: assume position-by-position edits
            for i, (o, n) in enumerate(zip(olds, news)):
                if difflib.SequenceMatcher(None, o.split(), n.split(), autojunk=False).ratio() >= 0.5:
                    pairs[i] = i
        return pairs
    for i, o in enumerate(olds):
        best, bj = 0.6, None
        for j, n in enumerate(news):
            if j in used:
                continue
            m = difflib.SequenceMatcher(None, o.split(), n.split(), autojunk=False)
            if m.real_quick_ratio() < best or m.quick_ratio() < best:
                continue
            r = m.ratio()
            if r > best:
                best, bj = r, j
        if bj is not None:
            pairs[i] = bj
            used.add(bj)
    return pairs


def line_diff(a, b):
    """Row lists for the old and new side ((kind, text, partner)) plus an ordered change list."""
    orows, nrows, changes = [], [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        olds, news = a[i1:i2], b[j1:j2]
        if tag == "equal":
            orows += [("same", x, None) for x in olds]
            nrows += [("same", x, None) for x in news]
            continue
        pairs = _pair_lines(olds, news) if (olds and news) else {}
        partner_of_new = {j: i for i, j in pairs.items()}
        for i, o in enumerate(olds):
            if i in pairs:
                orows.append(("chg", o, news[pairs[i]]))
                changes.append(("pair", o, news[pairs[i]]))
            else:
                orows.append(("del", o, None))
                changes.append(("del", o))
        for j, n in enumerate(news):
            if j in partner_of_new:
                nrows.append(("chg", n, olds[partner_of_new[j]]))
            else:
                nrows.append(("ins", n, None))
                changes.append(("ins", n))
    return orows, nrows, changes


def compare(old_text, new_text):
    om, oi, ol = parse_snapshot(old_text)
    nm, ni, nl = parse_snapshot(new_text)
    rows_old, rows_new, changes = line_diff(ol, nl)
    return {
        "old": (om, oi, ol), "new": (nm, ni, nl),
        "meta": [(k, om.get(k), nm.get(k)) for k in sorted(set(om) | set(nm)) if om.get(k) != nm.get(k)],
        "img_added": [u for u in ni if u not in set(oi)],
        "img_removed": [u for u in oi if u not in set(ni)],
        "rows_old": rows_old, "rows_new": rows_new, "changes": changes,
        "counts": {"edited": sum(1 for c in changes if c[0] == "pair"),
                   "removed": sum(1 for c in changes if c[0] == "del"),
                   "added": sum(1 for c in changes if c[0] == "ins")},
        "prices": (sorted(set(PRICE.findall(old_text or ""))), sorted(set(PRICE.findall(new_text or "")))),
    }


# ---------------------------------------------------------------- Discord message
def _summary(d):
    parts = []
    c = d["counts"]
    t = [f"{c[k]} {k}" for k in ("edited", "removed", "added") if c[k]]
    if t:
        parts.append("text (" + ", ".join(t) + ")")
    if d["img_added"] or d["img_removed"]:
        i = []
        if d["img_added"]:
            i.append(f"{len(d['img_added'])} added")
        if d["img_removed"]:
            i.append(f"{len(d['img_removed'])} removed")
        parts.append("images (" + ", ".join(i) + ")")
    if d["meta"]:
        parts.append("page info (" + ", ".join(k for k, _, _ in d["meta"][:5]) + ")")
    return " | ".join(parts) or "minor changes"


def _prices_line(d):
    po, pn = d["prices"]
    gone, new = [p for p in po if p not in pn], [p for p in pn if p not in po]
    if gone or new:
        return f"Prices: {', '.join(gone) or '-'} -> {', '.join(new) or '-'}"
    return f"Prices: unchanged ({', '.join(pn)})" if pn else ""


def _disp(line):
    """Readable form of a saved line for the Discord message (no [link]/[text] tags)."""
    m = LINK_LINE.match(line)
    if m:
        sp = urlsplit(m.group(2))
        return f"{m.group(1)} -> {sp.netloc}{sp.path}"
    return _plain(line)


def _meta_val(v):
    return v if re.match(r"https?://\S+$", v) else cut(v, 90)


def _compose(name, url, stamp, d, kind, max_text, max_img, max_meta, attached=frozenset(), exact=False):
    out = [f"**{name}** {kind} ({stamp})", url, "", f"What changed: {_summary(d)}"]
    pl = _prices_line(d)
    if pl:
        out.append(pl)

    meta_lines = []
    for k, ov, nv in d["meta"][:max_meta]:
        if ov is not None and nv is not None and not (re.match(r"https?://\S+$", ov) or re.match(r"https?://\S+$", nv)):
            a, b = snippet_pair(ov, nv, full=90)
            meta_lines += [f"- {k}: {a}", f"+ {k}: {b}"]
        else:
            if ov is not None:
                meta_lines.append(f"- {k}: {_meta_val(ov)}")
            if nv is not None:
                meta_lines.append(f"+ {k}: {_meta_val(nv)}")
    if meta_lines:
        out += ["", "Page info", "```diff"] + meta_lines + ["```"]

    text_lines, shown = [], 0
    for ch in d["changes"]:
        if shown >= max_text:
            break
        shown += 1
        if ch[0] == "pair":
            a, b = snippet_pair(_disp(ch[1]), _disp(ch[2]))
            text_lines += ["- " + cut(a, 150), "+ " + cut(b, 150)]
        elif ch[0] == "del":
            text_lines.append("- " + cut(_disp(ch[1]), 150))
        else:
            text_lines.append("+ " + cut(_disp(ch[1]), 150))
    if text_lines:
        more = len(d["changes"]) - shown
        if more > 0:
            text_lines.append(f"  … and {more} more text changes")
        out += ["", "Text", "```diff"] + text_lines + ["```"]

    # attached images are <wrapped> (Discord already shows them inline), new images that could not be
    # attached are plain links so Discord shows a preview
    img_lines = [f"NEW <{u}> (attached)" if u in attached else f"NEW {u}" for u in d["img_added"][:max_img]]
    img_lines += [f"REMOVED <{u}> (attached)" if u in attached else f"REMOVED <{u}>"
                  for u in d["img_removed"][:max_img]]
    extra = max(0, len(d["img_added"]) - max_img) + max(0, len(d["img_removed"]) - max_img)
    if extra:
        img_lines.append(f"… and {extra} more images (all of them are in the attached previews)")
    if img_lines:
        out += ["", "Images"] + img_lines
    if exact:
        out += ["", "Attached: exact copies of the page, before and after (download and open in a browser). "
                    "All images and the full list of changes are at the very bottom of each file."]
    else:
        out += ["", "Attached: before and after previews (download and open in a browser)."]
    return "\n".join(out)


def build_message(name, url, stamp, d, kind="changed", limit=1850, attached=frozenset(), exact=False):
    max_text, max_img, max_meta = 12, 4, 4
    while True:
        msg = _compose(name, url, stamp, d, kind, max_text, max_img, max_meta, attached, exact)
        if len(msg) <= limit:
            return msg
        if max_text > 2:
            max_text = max(2, max_text - 3)
        elif max_img > 2:
            max_img = 2
        elif max_meta > 1:
            max_meta = 1
        else:  # last resort, keep code fences balanced
            cut_msg = msg[:limit]
            if cut_msg.count("```") % 2:
                cut_msg = cut_msg.rsplit("```", 1)[0].rstrip() + "\n```"
            return cut_msg


# ---------------------------------------------------------------- HTML previews
CSS = """
:root{--bg:#fff;--fg:#1c1c21;--mut:#6b6b76;--card:#f4f4f7;--bd:#dcdce3;--del-bg:#fde9e9;--del-bd:#d93a3f;--ins-bg:#e4f6e8;--ins-bd:#238c42;--chg-bg:#fff6d9;--chg-bd:#d9a400;--link:#0a58ca}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#ececf1;--mut:#9a9aa6;--card:#202026;--bd:#34343d;--del-bg:#3a1f22;--del-bd:#ff6369;--ins-bg:#1b3324;--ins-bd:#4cc76d;--chg-bg:#3a3217;--chg-bd:#f0c33c;--link:#6ea8ff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--link)}
.bar{position:sticky;top:0;z-index:5;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 18px;background:var(--card);border-bottom:4px solid var(--bd)}
.bar.before{border-bottom-color:var(--del-bd)}.bar.after{border-bottom-color:var(--ins-bd)}
.tag{font-weight:700;letter-spacing:.06em;padding:3px 10px;border-radius:6px;color:#fff;background:#666}
.before .tag{background:var(--del-bd)}.after .tag{background:var(--ins-bd)}
.when{color:var(--mut);margin-left:auto}
main{max-width:1100px;margin:0 auto;padding:18px}
h2{margin:28px 0 10px;font-size:18px;border-bottom:1px solid var(--bd);padding-bottom:6px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0}
.chip{padding:2px 10px;border-radius:999px;background:var(--card);border:1px solid var(--bd);font-size:13px;text-decoration:none;color:var(--fg)}
.note{color:var(--mut);margin:6px 0}
.sm{color:var(--mut);font-size:12px;word-break:break-all}
table{border-collapse:collapse;width:100%}
td{padding:6px 10px;border-bottom:1px solid var(--bd);vertical-align:top;word-break:break-word}
td.k{color:var(--mut);white-space:nowrap;width:1%}
.row-del,.line.del,.lg.del{background:var(--del-bg);box-shadow:inset 4px 0 var(--del-bd)}
.row-ins,.line.ins,.lg.ins{background:var(--ins-bg);box-shadow:inset 4px 0 var(--ins-bd)}
.row-chg,.line.chg,.lg.chg{background:var(--chg-bg);box-shadow:inset 4px 0 var(--chg-bd)}
.line{padding:4px 10px;margin:2px 0;border-radius:4px;word-break:break-word}
.lg{display:inline-block;padding:2px 10px;border-radius:4px;font-size:12px;margin-right:6px}
del{background:var(--del-bd);color:#fff;text-decoration:line-through;border-radius:3px;padding:0 2px}
ins{background:var(--ins-bd);color:#fff;text-decoration:none;border-radius:3px;padding:0 2px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
.card{border:3px solid var(--bd);border-radius:8px;overflow:hidden;background:var(--card)}
.card.del{border-color:var(--del-bd)}.card.ins{border-color:var(--ins-bd)}
.card img{display:block;width:100%;height:130px;object-fit:cover}
.noimg{display:none;height:130px;align-items:center;justify-content:center;color:var(--mut);font-size:13px}
.cap{padding:6px 8px;font-size:12px;color:var(--mut);word-break:break-all}
.badge{display:inline-block;margin-right:6px;padding:0 6px;border-radius:4px;font-weight:700;color:#fff}
.card.del .badge{background:var(--del-bd)}.card.ins .badge{background:var(--ins-bd)}
.thumb{max-height:80px;max-width:240px;border-radius:4px;vertical-align:middle;margin-right:8px}
"""
ONERR = "this.style.display='none';this.nextElementSibling.style.display='flex'"


def _is_image_url(v):
    return bool(re.match(r"https?://\S+$", v)) and bool(IMG_RE.search(v))


def _value_html(v):
    if _is_image_url(v):
        return (f'<img class="thumb" src="{esc(v)}" referrerpolicy="no-referrer" alt="" onerror="this.style.display=\'none\'">'
                f'<a href="{esc(v)}">{esc(v)}</a>')
    if re.match(r"https?://\S+$", v):
        return f'<a href="{esc(v)}">{esc(v)}</a>'
    return esc(v)


def _plain(t):
    return t[7:] if t.startswith("[text] ") else t


def _line_html(kind, text, other, side):
    if kind == "chg":
        a, b = (text, other) if side == "before" else (other, text)
        ha, hb = word_diff_html(_plain(a), _plain(b))
        return ha if side == "before" else hb
    m = LINK_LINE.match(text)
    if m:
        return f'<a href="{esc(m.group(2))}">{esc(m.group(1))}</a> <span class="sm">{esc(m.group(2))}</span>'
    return esc(_plain(text))


def render_preview(name, url, side, stamp, d, highlight=True, note="", embed=None):
    """One self-contained HTML page showing the page content as it was (before) or is (after)."""
    om, oi, _ = d["old"]
    nm, ni, _ = d["new"]
    meta, imgs = (om, oi) if side == "before" else (nm, ni)
    other_meta = nm if side == "before" else om
    other_imgs = set(ni if side == "before" else oi)
    rows = d["rows_old"] if side == "before" else d["rows_new"]
    own_kind = "del" if side == "before" else "ins"
    chips = []

    def anchor(label):
        n = len(chips) + 1
        chips.append(f'<a class="chip" href="#c{n}">{esc(label)}</a>')
        return f' id="c{n}"'

    meta_rows = []
    for k in sorted(meta):
        v, ov = meta[k], other_meta.get(k)
        cls, aid, vhtml = "", "", _value_html(v)
        if highlight and ov is None:
            cls, aid = f' class="row-{own_kind}"', anchor(f"page info: {k}")
        elif highlight and ov != v:
            a, b = (v, ov) if side == "before" else (ov, v)
            ha, hb = word_diff_html(a, b)
            vhtml = ha if side == "before" else hb
            cls, aid = ' class="row-chg"', anchor(f"page info: {k}")
        meta_rows.append(f'<tr{cls}{aid}><td class="k">{esc(k)}</td><td>{vhtml}</td></tr>')

    line_rows = []
    for kind, text, other in rows:
        if not highlight or kind == "same":
            line_rows.append(f'<div class="line">{_line_html("same", text, None, side)}</div>')
            continue
        cls = {"del": "del", "ins": "ins", "chg": "chg"}[kind]
        line_rows.append(f'<div class="line {cls}"{anchor("text: " + cut(_plain(text), 28))}>'
                         f'{_line_html(kind, text, other, side)}</div>')

    cards = []
    for u in imgs:
        flagged = highlight and u not in other_imgs
        cls = f" {own_kind}" if flagged else ""
        aid = anchor("image: " + short_img(u).split("/")[-1]) if flagged else ""
        badge = f'<span class="badge">{"REMOVED" if side == "before" else "NEW"}</span>' if flagged else ""
        cards.append(
            f'<div class="card{cls}"{aid}><a href="{esc(u)}" target="_blank" rel="noreferrer">'
            f'<img src="{esc((embed or {}).get(u, u))}" loading="lazy" referrerpolicy="no-referrer" alt="" onerror="{ONERR}">'
            f'<div class="noimg">image unavailable</div></a>'
            f'<div class="cap">{badge}<a href="{esc(u)}" target="_blank" rel="noreferrer">{esc(u)}</a></div></div>')

    c = d["counts"]
    summary = _summary(d) if highlight else "full page content"
    legend = ('<span class="lg del">removed</span><span class="lg chg">edited</span>'
              '<span class="lg ins">added</span>') if highlight else ""
    tag = "BEFORE" if side == "before" else "AFTER"
    chip_html = f'<div class="chips">{"".join(chips[:60])}</div>' if chips else ""
    note_html = f'<p class="note">{esc(note)}</p>' if note else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(name)} {tag.lower()}</title><style>{CSS}</style></head>
<body>
<header class="bar {side}"><span class="tag">{tag}</span><strong>{esc(name)}</strong>
<a href="{esc(url)}" target="_blank" rel="noreferrer">{esc(url)}</a><span class="when">{esc(stamp)}</span></header>
<main>
<p><strong>{esc(summary)}</strong></p>{note_html}
<div>{legend}</div>{chip_html}
<h2>Page info ({len(meta_rows)})</h2><table>{"".join(meta_rows) or '<tr><td class="sm">none</td></tr>'}</table>
<h2>Content ({len(line_rows)} lines)</h2><div>{"".join(line_rows) or '<p class="sm">no text</p>'}</div>
<h2>Images ({len(cards)})</h2><div class="grid">{"".join(cards)}</div>
</main></body></html>
"""


# ---------------------------------------------------------------- exact copies of pages
def prepare_copy(raw, url):
    """Turn saved page HTML into a standalone file that looks like the live page when opened offline:
    scripts removed (they would re-render or break the page), lazy-loaded images switched on,
    and a <base> tag so relative stylesheets and images still load from the original site."""
    s = re.sub(r"<script\b.*?</script\s*>", "", raw, flags=re.S | re.I)
    s = re.sub(r"<meta\b[^>]*http-equiv\s*=\s*[\"']?content-security-policy[^>]*>", "", s, flags=re.I)

    def lazy(m):
        tag = m.group(0)
        for lazy_attr, real in (("data-src", "src"), ("data-srcset", "srcset")):
            mm = re.search(rf'\s{lazy_attr}\s*=\s*"([^"]*)"', tag, re.I)
            if not mm:
                continue
            val = mm.group(1)
            if re.search(rf'\s{real}\s*=\s*"[^"]*"', tag, re.I):
                tag = re.sub(rf'(\s{real}\s*=\s*)"[^"]*"', lambda g: f'{g.group(1)}"{val}"', tag, count=1, flags=re.I)
            else:
                tag = re.sub(r"^<(img|source)\b", lambda g: f'<{g.group(1)} {real}="{val}"', tag, count=1, flags=re.I)
        return tag
    s = re.sub(r"<(?:img|source)\b[^>]*>", lazy, s, flags=re.I)
    inject = f'<meta charset="utf-8"><base href="{esc(url)}">'
    m = re.search(r"<head\b[^>]*>", s, re.I)
    return (s[:m.end()] + inject + s[m.end():]) if m else (inject + s)


REPORT_CSS = """
#psw{all:initial;display:block;flex:0 0 100%;width:100%;clear:both;position:relative;z-index:2147483000;background:#fff;color:#1c1c21;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:28px 24px 56px;border-top:8px solid #888;box-sizing:border-box;text-align:left}
#psw *{box-sizing:border-box;font-family:inherit;float:none;position:static;transform:none;max-width:100%}
#psw.before{border-top-color:#d93a3f}#psw.after{border-top-color:#238c42}
#psw h1{display:block;margin:0 0 6px;font-size:24px;line-height:1.3;font-weight:700;color:#1c1c21;text-transform:none;letter-spacing:0}
#psw h2{display:block;margin:30px 0 10px;padding-bottom:6px;font-size:19px;line-height:1.3;font-weight:700;color:#1c1c21;border-bottom:1px solid #dcdce3;text-transform:none;letter-spacing:0}
#psw h3{display:block;margin:20px 0 8px;font-size:16px;font-weight:700;color:#1c1c21;text-transform:none;letter-spacing:0}
#psw p,#psw .sub{display:block;margin:4px 0;font-size:14px;color:#55555f}
#psw a{color:#0a58ca;text-decoration:underline;word-break:break-all}
#psw .tag{display:inline-block;padding:2px 12px;border-radius:6px;color:#fff;background:#666;font-size:16px;letter-spacing:.06em;vertical-align:middle}
#psw.before .tag{background:#d93a3f}#psw.after .tag{background:#238c42}
#psw .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:12px}
#psw .card{display:block;border:3px solid #dcdce3;border-radius:8px;overflow:hidden;background:#f4f4f7}
#psw .card.del{border-color:#d93a3f}#psw .card.ins{border-color:#238c42}
#psw .card img{display:block;width:100%;height:130px;object-fit:cover;margin:0;border:0}
#psw .noimg{display:none;height:130px;align-items:center;justify-content:center;color:#6b6b76;font-size:13px}
#psw .cap{display:block;padding:6px 8px;font-size:12px;color:#55555f;word-break:break-all}
#psw .badge{display:inline-block;margin-right:6px;padding:0 6px;border-radius:4px;font-weight:700;color:#fff;background:#666}
#psw .card.del .badge{background:#d93a3f}#psw .card.ins .badge{background:#238c42}
#psw table{display:table;border-collapse:collapse;width:100%;margin:0}
#psw td{display:table-cell;padding:6px 10px;border-bottom:1px solid #dcdce3;vertical-align:top;word-break:break-word;font-size:14px;color:#1c1c21}
#psw td.k{color:#6b6b76;white-space:nowrap;width:1%}
#psw .line{display:block;margin:3px 0;padding:5px 10px;border-radius:4px;font-size:14px;color:#1c1c21;word-break:break-word}
#psw .line.del{background:#fde9e9;box-shadow:inset 4px 0 #d93a3f}
#psw .line.ins{background:#e4f6e8;box-shadow:inset 4px 0 #238c42}
#psw .line.chg{background:#fff6d9;box-shadow:inset 4px 0 #d9a400}
#psw del{background:#d93a3f;color:#fff;text-decoration:line-through;border-radius:3px;padding:0 2px}
#psw ins{background:#238c42;color:#fff;text-decoration:none;border-radius:3px;padding:0 2px}
#psw .imgrow{display:flex;align-items:center;gap:10px;margin:6px 0;font-size:13px}
#psw .thumb{display:block;width:90px;height:60px;object-fit:cover;border-radius:4px;flex:0 0 auto}
#psw .none{display:block;color:#6b6b76;font-style:italic;margin:6px 0}
"""


def _gallery(imgs, flagged, side, embed):
    own = "del" if side == "before" else "ins"
    word = "REMOVED" if side == "before" else "NEW"
    cards = []
    for u in imgs:
        hit = u in flagged
        cards.append(
            f'<div class="card{" " + own if hit else ""}"><a href="{esc(u)}" target="_blank" rel="noreferrer">'
            f'<img src="{esc(embed.get(u, u))}" loading="lazy" referrerpolicy="no-referrer" alt="" onerror="{ONERR}">'
            f'<div class="noimg">image unavailable</div></a>'
            f'<div class="cap">{f"<span class=badge>{word}</span>" if hit else ""}'
            f'<a href="{esc(u)}" target="_blank" rel="noreferrer">{esc(u)}</a></div></div>')
    return "".join(cards)


def _changes(side, d, embed):
    """The 'what was removed' (before file) / 'what was added' (after file) section."""
    before = side == "before"
    word = "Removed" if before else "Added"
    rows = d["rows_old"] if before else d["rows_new"]
    imgs = d["img_removed"] if before else d["img_added"]
    out = [f"<h2>{word} (everything that changed on this page)</h2>"]

    meta_rows = []
    for k, ov, nv in d["meta"]:
        mine, other = (ov, nv) if before else (nv, ov)
        if mine is None:
            continue
        if other is None:
            vhtml = _value_html(mine)
        else:
            ha, hb = word_diff_html(ov, nv)
            vhtml = ha if before else hb
        meta_rows.append(f'<tr><td class="k">{esc(k)}</td><td>{vhtml}</td></tr>')
    if meta_rows:
        out.append(f"<h3>Page info ({len(meta_rows)})</h3><table>{''.join(meta_rows)}</table>")

    lines = [f'<div class="line {kind}">{_line_html(kind, text, other, side)}</div>'
             for kind, text, other in rows if kind != "same"]
    if lines:
        out.append(f"<h3>Text ({len(lines)} lines)</h3>{''.join(lines)}")

    if imgs:
        items = "".join(
            f'<div class="imgrow"><img class="thumb" src="{esc(embed.get(u, u))}" referrerpolicy="no-referrer" alt="" '
            f'onerror="this.style.visibility=\'hidden\'"><a href="{esc(u)}" target="_blank" rel="noreferrer">{esc(u)}</a></div>'
            for u in imgs)
        out.append(f"<h3>Images ({len(imgs)})</h3>{items}")

    if not (meta_rows or lines or imgs):
        out.append(f'<span class="none">Nothing was {word.lower()}.</span>')
    return "".join(out)


def _report(side, name, url, stamp, d, embed, imgs, note, highlight, what):
    tag = "BEFORE" if side == "before" else "AFTER"
    flagged = set(d["img_removed"] if side == "before" else d["img_added"]) if highlight else set()
    out = [f'<style>{REPORT_CSS}</style><div id="psw" class="{side}">',
           f'<h1><span class="tag">{tag}</span> {esc(name)}</h1>',
           f'<p class="sub">{esc(what)} ({esc(stamp)}) <a href="{esc(url)}" target="_blank" rel="noreferrer">{esc(url)}</a></p>']
    if note:
        out.append(f'<p class="sub">{esc(note)}</p>')
    out.append(f'<h2>All images on this page ({len(imgs)})</h2><div class="grid">{_gallery(imgs, flagged, side, embed)}</div>')
    if highlight:
        out.append(_changes(side, d, embed))
    out.append("</div>")
    return "".join(out)


def copy_file(side, raw, url, name, stamp, d, embed=None, note="", prepared=False, highlight=True, what=None):
    """The page itself, exactly as saved, with an extra section added at the very end: every image on
    the page, then (before file) what was removed or (after file) what was added."""
    embed = embed or {}
    page = raw if prepared else prepare_copy(raw, url)
    what = what or ("Exact copy of the page as it was before the change" if side == "before"
                    else "Exact copy of the page as it is now")
    imgs = image_urls(raw)
    report = _report(side, name, url, stamp, d, embed, imgs, note, highlight, what)
    i = page.lower().rfind("</body>")
    page = (page[:i] + report + page[i:]) if i != -1 else (page + report)
    return page.encode("utf-8")


# ---------------------------------------------------------------- alert builders
def file_stub(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "page"


def change_alert(name, url, stamp, old_text, new_text, kind="changed", note="", stub=None, images=None,
                 raw_before=None, raw_after=None, prepared=False):
    """images: optional {url: bytes} for changed images (attached to the Discord message and embedded in
    the files). raw_before / raw_after: the page HTML before and after, which gives exact copies; without
    both, a rebuilt preview is attached instead."""
    d = compare(old_text, new_text)
    stub = file_stub(stub or name)
    images = images or {}
    extras, attached, used = [], set(), 0
    for label, urls in (("new", d["img_added"]), ("removed", d["img_removed"])):
        n = 0
        for u in urls:
            data = images.get(u)
            if not data or n >= MAX_ATTACH or used + len(data) > ATTACH_BYTES:
                continue
            mime, ext = sniff(data)
            if mime == "application/octet-stream":
                continue
            base = urlsplit(u).path.rstrip("/").split("/")[-1].rpartition(".")[0] or "image"
            extras.append((f"{label}_{n + 1}_{file_stub(base)[:40]}{ext}", data))
            attached.add(u)
            used += len(data)
            n += 1
    embed, tot = {}, 0
    for u in d["img_added"] + d["img_removed"]:
        data = images.get(u)
        if not data or len(data) > EMBED_ONE or tot + len(data) > EMBED_TOTAL:
            continue
        mime, _ = sniff(data)
        if mime != "application/octet-stream":
            embed[u] = f"data:{mime};base64," + base64.b64encode(data).decode()
            tot += len(data)
    exact = bool(raw_before and raw_after)
    if exact:
        files = [(f"{stub}_before.html", copy_file("before", raw_before, url, name, stamp, d, embed, note, prepared)),
                 (f"{stub}_after.html", copy_file("after", raw_after, url, name, stamp, d, embed, note, prepared))]
    else:
        n2 = (note + " " if note else "") + "(No saved copy of the earlier version yet, so this is a rebuilt preview, not an exact copy.)"
        files = [(f"{stub}_before.html", render_preview(name, url, "before", stamp, d, note=n2, embed=embed).encode()),
                 (f"{stub}_after.html", render_preview(name, url, "after", stamp, d, note=n2, embed=embed).encode())]
    return {"text": build_message(name, url, stamp, d, kind, attached=attached, exact=exact),
            "files": files, "extra": extras}


def live_alert(name, url, stamp, new_text, note="", raw=None):
    d = compare("", new_text)
    meta, imgs, lines = d["new"]
    title = meta.get("og:title") or meta.get("title") or ""
    body = [l for l in lines if len(l) > 3][:8]
    out = [f"**{name}** is now LIVE ({stamp})", url, ""]
    if title:
        out.append(f"Title: {cut(title, 120)}")
    out.append(f"{len(lines)} text lines, {len(imgs)} images")
    if body:
        out += ["", "```"] + [cut(_plain(l), 150) for l in body] + ["```"]
    if raw:
        out += ["", "Attached: exact copy of the page (download and open in a browser). All images are at the very bottom."]
        data = copy_file("after", raw, url, name, stamp, d, {}, note, highlight=False)
    else:
        out += ["", "Attached: full page preview (download and open in a browser)."]
        data = render_preview(name, url, "after", stamp, d, highlight=False, note=note).encode()
    return {"text": "\n".join(out)[:1850], "files": [(f"{file_stub(name)}_live.html", data)]}


def gone_alert(name, url, stamp, old_text, status="404", note="", raw=None):
    d = compare(old_text, "")
    files = []
    if raw:
        files = [(f"{file_stub(name)}_last_seen.html",
                  copy_file("before", raw, url, name, stamp, d, {}, note, highlight=False,
                            what="Exact copy of the last version we saw"))]
    elif old_text:
        files = [(f"{file_stub(name)}_last_seen.html",
                  render_preview(name, url, "before", stamp, d, highlight=False, note=note).encode())]
    out = [f"**{name}** is gone (HTTP {status}) ({stamp})", url]
    if files:
        out += ["", "Attached: the last version we saw (download and open in a browser)."]
    return {"text": "\n".join(out), "files": files}


# ---------------------------------------------------------------- Discord delivery
def split_text(text, limit=1900):
    """Split a long message at line boundaries into chunks that fit Discord's limit, keeping ``` blocks balanced."""
    if len(text) <= limit:
        return [text]
    room = limit - 8                       # keeps space for a closing fence
    lines = []
    for raw in text.split("\n"):
        while len(raw) > room - 40:        # one very long line: hard split it
            lines.append(raw[:room - 40])
            raw = raw[room - 40:]
        lines.append(raw)
    chunks, cur, size, fence = [], [], 0, None

    def flush():
        nonlocal cur, size
        chunks.append("\n".join(cur) + ("\n```" if fence else ""))
        cur = [fence] if fence else []
        size = sum(len(x) + 1 for x in cur)

    for line in lines:
        if cur and size + len(line) + 1 > room:
            flush()
        cur.append(line)
        size += len(line) + 1
        if line.strip().startswith("```"):
            fence = None if fence else line.strip()
    if cur:
        chunks.append("\n".join(cur))
    return chunks


def _multipart(payload, files):
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f'Content-Type: application/json\r\n\r\n').encode() + json.dumps(payload).encode() + b"\r\n"
    for i, (fname, data) in enumerate(files):
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        head = (f'--{b}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n'
                f'Content-Type: {ctype}\r\n\r\n')
        body += head.encode() + data + b"\r\n"
    body += f"--{b}--\r\n".encode()
    return body, f"multipart/form-data; boundary={b}"


def _post(hook, payload, files):
    if files:
        body, ctype = _multipart(payload, files)
    else:
        body, ctype = json.dumps(payload).encode(), "application/json"
    req = urllib.request.Request(hook, data=body, headers={"Content-Type": ctype, "User-Agent": UA})
    for attempt in range(2):
        try:
            urllib.request.urlopen(req, timeout=30).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:  # rate limited, wait and retry once
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 10) + 0.5)
                continue
            print(f"discord post failed: HTTP {e.code}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"discord post failed: {type(e).__name__}", file=sys.stderr)
            return False
    return False


def send_alerts(alerts, ping_text="@everyone"):
    """Print every alert to the log and send each one to Discord. Every alert pings; long ones are split
    into several messages (only the first pings and carries the attachments), nothing is cut off."""
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    for a in alerts:
        files = a["files"] + a.get("extra", [])
        print(a["text"] + ("\n[attachments: " + ", ".join(f for f, _ in files) + "]" if files else ""), flush=True)
        print("-" * 40)
        if not hook:
            continue
        ping = (ping_text + "\n") if ping_text else ""
        text = a["text"]
        if sum(len(d) for _, d in files) > UPLOAD_LIMIT:  # too big: drop the image attachments first
            files = a["files"]
        if sum(len(d) for _, d in files) > UPLOAD_LIMIT:
            files = []
            text += "\n(previews too large to attach)"
        for i, part in enumerate(split_text(text)):
            payload = {"content": ((ping if i == 0 else "") + part)[:2000]}
            payload["allowed_mentions"] = {"parse": ["everyone"] if (ping and i == 0) else []}
            send_files = files if i == 0 else []
            if not _post(hook, payload, send_files) and send_files:  # retry without files so the alert still lands
                _post(hook, payload, [])
            time.sleep(1)
