"""Tiny Markdown -> HTML converter for the CHANGELOG preview (the subset VIVER generates)."""
import html
import re


def _inline(t, img_base):
    t = html.escape(t, quote=False)
    t = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)",
               lambda m: '<img alt="%s" src="%s%s" loading="lazy">' % (m.group(1), img_base, m.group(2)), t)
    def _link(m):
        href = m.group(2)
        if not href.startswith(("#", "http://", "https://")):
            href = img_base + href
        return '<a href="%s" target="_blank">%s</a>' % (href, m.group(1)) if not m.group(2).startswith("#") else '<a href="%s">%s</a>' % (href, m.group(1))
    t = re.sub(r"(?<!!)\[((?:<img [^>]*>)|[^\]]+)\]\((#[A-Za-z0-9_-]+|https?://[^)\s]+|[^)\s]+\.(?:svg|png))\)", _link, t)
    t = t.replace("&lt;br&gt;", "<br>")
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])_([^_]+)_(?!\w)", r"<i>\1</i>", t)
    return t


def render(md, img_base=""):
    out, i = [], 0
    lines = md.splitlines()
    in_list = None
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i][2:] if lines[i].startswith("  ") else lines[i])
                i += 1
            rows = []
            for b in buf:
                cls = "add" if b.startswith("+") else "del" if b.startswith("-") else "hunk" if b.startswith("@@") else ""
                rows.append('<span class="%s">%s</span>' % (cls, html.escape(b)))
            out.append('<pre class="diff">%s</pre>' % "\n".join(rows))
            i += 1
            continue
        if in_list and not re.match(r"^(\d+\.|-)\s", s) and not raw.startswith("  "):
            out.append("</%s>" % in_list)
            in_list = None
        if not s:
            i += 1
            continue
        if s.startswith("<details") or s.startswith("</details") or s.startswith("<summary"):
            out.append(s)
        elif re.fullmatch(r'<a id="[A-Za-z0-9_-]+"></a>', s):
            out.append(s)
        elif s.startswith("#"):
            n = len(s) - len(s.lstrip("#"))
            out.append("<h%d>%s</h%d>" % (n, _inline(s[n:].strip(), img_base), n))
        elif s == "---":
            out.append("<hr>")
        elif s.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            h = "<table>"
            for ri, r in enumerate(rows):
                tag = "th" if ri == 0 else "td"
                h += "<tr>" + "".join("<%s>%s</%s>" % (tag, _inline(c, img_base), tag) for c in r) + "</tr>"
            out.append(h + "</table>")
            continue
        elif s.startswith(">"):
            out.append("<blockquote>%s</blockquote>" % _inline(s.lstrip("> "), img_base))
        elif re.match(r"^(\d+\.|-)\s", s) and not raw.startswith("  "):
            kind = "ol" if s[0].isdigit() else "ul"
            if in_list != kind:
                if in_list:
                    out.append("</%s>" % in_list)
                out.append("<%s>" % kind)
                in_list = kind
            out.append("<li>%s</li>" % _inline(re.sub(r"^(\d+\.|-)\s", "", s), img_base))
        elif raw.startswith("  ") and in_list:
            out.append('<div class="sub">%s</div>' % _inline(s.lstrip("> "), img_base))
        else:
            out.append("<p>%s</p>" % _inline(s, img_base))
        i += 1
    if in_list:
        out.append("</%s>" % in_list)
    return "\n".join(out)
