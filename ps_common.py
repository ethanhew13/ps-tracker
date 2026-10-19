#!/usr/bin/env python3
"""
Shared helpers for the PlayStation watchers (ps_page_watch.py, ps_feed_watch.py):
page parsing, change diffing, readable Discord summaries and before/after HTML previews.
Stdlib only. Keep this file in the same folder as the watcher scripts.
"""
import difflib
import html
import json
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
    return short_img(v) if (re.match(r"https?://\S+$", v) and IMG_RE.search(v)) else cut(v, 90)


def _compose(name, url, stamp, d, kind, max_text, max_img, max_meta):
    out = [f"**{name}** {kind} ({stamp})", url, "", f"What changed: {_summary(d)}"]
    pl = _prices_line(d)
    if pl:
        out.append(pl)

    meta_lines = []
    for k, ov, nv in d["meta"][:max_meta]:
        if ov is not None and nv is not None and not (_meta_val(ov) != cut(ov, 90) or _meta_val(nv) != cut(nv, 90)):
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

    img_lines = [f"+ {short_img(u)}" for u in d["img_added"][:max_img]]
    img_lines += [f"- {short_img(u)}" for u in d["img_removed"][:max_img]]
    extra = max(0, len(d["img_added"]) - max_img) + max(0, len(d["img_removed"]) - max_img)
    if extra:
        img_lines.append(f"  … and {extra} more images")
    if img_lines:
        out += ["", "Images", "```diff"] + img_lines + ["```"]
    out += ["", "Attached: before and after previews (download and open in a browser)."]
    return "\n".join(out)


def build_message(name, url, stamp, d, kind="changed", limit=1850):
    max_text, max_img, max_meta = 12, 6, 4
    while True:
        msg = _compose(name, url, stamp, d, kind, max_text, max_img, max_meta)
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


def render_preview(name, url, side, stamp, d, highlight=True, note=""):
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
            f'<img src="{esc(u)}" loading="lazy" referrerpolicy="no-referrer" alt="" onerror="{ONERR}">'
            f'<div class="noimg">image unavailable</div></a>'
            f'<div class="cap">{badge}{esc(short_img(u))}</div></div>')

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


# ---------------------------------------------------------------- alert builders
def file_stub(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:60] or "page"


def change_alert(name, url, stamp, old_text, new_text, kind="changed", note="", stub=None):
    d = compare(old_text, new_text)
    stub = file_stub(stub or name)
    return {
        "text": build_message(name, url, stamp, d, kind),
        "files": [(f"{stub}_before.html", render_preview(name, url, "before", stamp, d, note=note).encode()),
                  (f"{stub}_after.html", render_preview(name, url, "after", stamp, d, note=note).encode())],
    }


def live_alert(name, url, stamp, new_text, note=""):
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
    out += ["", "Attached: full page preview (download and open in a browser)."]
    return {"text": "\n".join(out)[:1850],
            "files": [(f"{file_stub(name)}_live.html",
                       render_preview(name, url, "after", stamp, d, highlight=False, note=note).encode())]}


def gone_alert(name, url, stamp, old_text, status="404", note=""):
    d = compare(old_text, "")
    files = []
    if old_text:
        files = [(f"{file_stub(name)}_last_seen.html",
                  render_preview(name, url, "before", stamp, d, highlight=False, note=note).encode())]
    out = [f"**{name}** is gone (HTTP {status}) ({stamp})", url]
    if files:
        out += ["", "Attached: the last version we saw (download and open in a browser)."]
    return {"text": "\n".join(out), "files": files}


# ---------------------------------------------------------------- Discord delivery
def _multipart(payload, files):
    b = uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f'Content-Type: application/json\r\n\r\n').encode() + json.dumps(payload).encode() + b"\r\n"
    for i, (fname, data) in enumerate(files):
        body += (f'--{b}\r\nContent-Disposition: form-data; name="files[{i}]"; filename="{fname}"\r\n'
                 f'Content-Type: text/html\r\n\r\n').encode() + data + b"\r\n"
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
    """Print every alert to the log and send each one to Discord (the first one pings)."""
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    for i, a in enumerate(alerts):
        print(a["text"] + ("\n[attachments: " + ", ".join(f for f, _ in a["files"]) + "]" if a["files"] else ""),
              flush=True)
        print("-" * 40)
        if not hook:
            continue
        ping = (ping_text + "\n") if (ping_text and i == 0) else ""
        payload = {"content": (ping + a["text"])[:2000]}
        payload["allowed_mentions"] = {"parse": ["everyone"] if ping else []}
        files = a["files"]
        if sum(len(d) for _, d in files) > 7_500_000:  # stay under Discord's upload limit
            files = []
            payload["content"] = (payload["content"] + "\n(previews too large to attach)")[:2000]
        if not _post(hook, payload, files) and files:  # retry without the files so the alert still lands
            _post(hook, payload, [])
        time.sleep(1)
