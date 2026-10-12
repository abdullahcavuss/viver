"""
Draws the block diagrams of the previous and new version in ONE SVG file (for the CHANGELOG).

- Being vector graphics, no cropping/tiling is needed; it stays sharp when zoomed in the browser.
- The drawing code is shared with render.Painter: SvgDraw mimics the part of Pillow's ImageDraw
  interface that is used and emits SVG elements.
- Wide diagrams place the two versions one above the other, narrow ones side by side.
"""
import html
import math

from . import render as R

SVG_SCALE = 2.0            # 1 LabVIEW pixel = 2 SVG units (so fonts are measured correctly)
GAP = 36                   # gap between the two versions (SVG units)
HEADER = 44                # height of the version header
FONT_FAMILY = "Segoe UI, Arial, DejaVu Sans, Liberation Sans, sans-serif"
MAX_VIEWS = 6
BADGE_R = 6.0              # badge radius in LabVIEW pixels (kept small so it does not hide the diagram)


def _c(col):
    """(r,g,b[,a]) -> (css color, opacity)"""
    if col is None:
        return "none", None
    if isinstance(col, str):
        return col, None
    if len(col) == 4:
        return "rgb(%d,%d,%d)" % col[:3], round(col[3] / 255.0, 3)
    return "rgb(%d,%d,%d)" % tuple(col[:3]), None


def _n(v):
    return ("%.1f" % v).rstrip("0").rstrip(".")


class SvgDraw:
    """ImageDraw-like interface; accumulates drawing operations as SVG text."""

    def __init__(self):
        self.out = []

    def _paint(self, fill=None, outline=None, width=1):
        a = []
        fc, fo = _c(fill)
        a.append('fill="%s"' % fc)
        if fo is not None:
            a.append('fill-opacity="%s"' % fo)
        if outline is not None:
            sc, so = _c(outline)
            a.append('stroke="%s" stroke-width="%s"' % (sc, _n(width)))
            if so is not None:
                a.append('stroke-opacity="%s"' % so)
        return " ".join(a)

    def rectangle(self, box, fill=None, outline=None, width=1):
        x0, y0, x1, y1 = box
        if x1 < x0:
            x0, x1 = x1, x0
        if y1 < y0:
            y0, y1 = y1, y0
        self.out.append('<rect x="%s" y="%s" width="%s" height="%s" %s/>' % (
            _n(x0), _n(y0), _n(x1 - x0), _n(y1 - y0), self._paint(fill, outline, width)))

    def rounded_rectangle(self, box, radius=0, fill=None, outline=None, width=1):
        x0, y0, x1, y1 = box
        self.out.append('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" %s/>' % (
            _n(x0), _n(y0), _n(x1 - x0), _n(y1 - y0), _n(radius), self._paint(fill, outline, width)))

    def ellipse(self, box, fill=None, outline=None, width=1):
        x0, y0, x1, y1 = box
        self.out.append('<ellipse cx="%s" cy="%s" rx="%s" ry="%s" %s/>' % (
            _n((x0 + x1) / 2), _n((y0 + y1) / 2), _n((x1 - x0) / 2), _n((y1 - y0) / 2), self._paint(fill, outline, width)))

    def line(self, pts, fill=None, width=1, joint=None):
        if len(pts) < 2:
            return
        sc, so = _c(fill)
        self.out.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="%s"%s%s/>' % (
            " ".join("%s,%s" % (_n(x), _n(y)) for x, y in pts), sc, _n(width),
            ' stroke-opacity="%s"' % so if so is not None else "",
            ' stroke-linejoin="round" stroke-linecap="round"' if joint else ""))

    def polygon(self, pts, fill=None, outline=None, width=1):
        self.out.append('<polygon points="%s" %s/>' % (
            " ".join("%s,%s" % (_n(x), _n(y)) for x, y in pts), self._paint(fill, outline, width)))

    def regular_polygon(self, circle, n, rotation=0, fill=None, outline=None):
        cx, cy, r = circle
        pts = []
        for i in range(n):
            a = math.radians(rotation + 360.0 * i / n - 90)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        self.polygon(pts, fill=fill, outline=outline)

    def textlength(self, text, font=None):
        return font.getlength(text) if font is not None else len(text) * 6

    def text(self, xy, text, font=None, fill=(0, 0, 0)):
        size = getattr(font, "size", 10)
        bold = getattr(font, "path", None) in R._FONT_FILES[True]
        sc, so = _c(fill)
        self.out.append('<text x="%s" y="%s" font-size="%s"%s fill="%s"%s>%s</text>' % (
            _n(xy[0]), _n(xy[1] + size * 0.92), _n(size), ' font-weight="bold"' if bold else "", sc,
            ' fill-opacity="%s"' % so if so is not None else "", html.escape(str(text), quote=False)))


def _boost(width):
    """A wide diagram shrinks when fitted to the page; scale badges and headers up proportionally."""
    return max(1.0, min(3.0, width / 2600.0))


def _paint_side(p, dim=True):
    """SVG counterpart of Painter.render(). Returns: SVG elements (str)."""
    d = SvgDraw()
    nodes, wires, notes, structs = p.visible_items()
    p.term_wire = {}
    for w in wires:
        for e in w["ends"]:
            p.term_wire.setdefault(e["node"], w)
    for n in structs:
        p.draw_structure(d, n)
    for w in wires:
        p.draw_wire(d, w)
    for n in nodes:
        p.draw_node(d, n)
    for t in notes:
        p.draw_note(d, t)
    for n in structs:
        p.draw_selector(d, n)
        p.draw_struct_terms(d, n)
    if dim and p.hl:
        d.out.append('<rect x="0" y="0" width="%s" height="%s" fill="#fff" fill-opacity="0.55"/>' % (_n(p.W), _n(p.H)))
        for w in wires:
            if w.get("uid") in p.hl:
                p.draw_wire(d, w)
        for n in nodes:
            if n["uid"] in p.hl:
                p.draw_node(d, n)
        for t in notes:
            if t.get("uid") in p.hl:
                p.draw_note(d, t)
        for n in structs:
            p.draw_selector(d, n)
    # highlights
    badges = []
    for w in wires:
        h = p.hl.get(w.get("uid"))
        if not h:
            continue
        col = R.ST[h[0]]
        best, bl = None, -1
        for seg in p.wire_path(w):
            d.line([p.P(*q) for q in seg], fill=col + (110,), width=p.lw(6), joint="curve")
            for (ax, ay), (bx, by) in zip(seg, seg[1:]):
                ln = abs(bx - ax) + abs(by - ay)
                if ln > bl:
                    best, bl = ((ax + bx) / 2, (ay + by) / 2), ln
        if best:
            badges.append((h, best[0], best[1], False))
    for item in nodes + structs + notes:
        h = p.hl.get(item.get("uid"))
        if not h:
            continue
        col = R.ST[h[0]]
        pad = 4
        iw = max(item["w"], 20) if item in notes else item["w"]
        ih = max(item["h"], 14) if item in notes else item["h"]
        box = p.R(item["x"] - pad, item["y"] - pad, iw + 2 * pad, ih + 2 * pad)
        filled = item not in structs
        d.rounded_rectangle(box, radius=4 * p.s, outline=col + (255,), width=p.lw(2.2),
                            fill=(col + (40,)) if filled else None)
        badges.append((h, item["x"] - pad, item["y"] - pad, True))
    seen, placed = set(), []
    r = BADGE_R * p.s
    for (st, num), x, y, corner in badges:
        if num in seen:
            continue
        seen.add(num)
        cx, cy = p.P(x, y)
        # keep the badge off the item: outside the top-left corner of a box, above a wire
        if corner:
            cx -= r * 0.85
            cy -= r * 0.85
        else:
            cy -= r + p.lw(3)
        for _ in range(12):
            hit = next(((px, py) for px, py in placed if abs(px - cx) < 2.1 * r and abs(py - cy) < 2.1 * r), None)
            if hit is None:
                break
            cx = hit[0] + 2.2 * r
        placed.append((cx, cy))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=R.ST[st], outline=(255, 255, 255), width=p.lw(1.2))
        fs = (7.5 if num < 100 else 6.2) * p.s
        d.out.append('<text x="%s" y="%s" font-size="%s" font-weight="bold" fill="#fff" text-anchor="middle">%d</text>' % (
            _n(cx), _n(cy + fs * 0.36), _n(fs), num))
    return d.out


def _side_painter(bd, assign, hl, names):
    b = R.robust_bounds(bd)
    x0, y0 = b[0] - R.MARGIN, b[1] - R.MARGIN
    W = max(1.0, b[2] - b[0]) + 2 * R.MARGIN
    H = max(1.0, b[3] - b[1]) + 2 * R.MARGIN
    p = R.Painter(bd, assign, hl, (x0, y0), 1.0, (1, 1), names)
    p.s = SVG_SCALE
    p.W, p.H = W * SVG_SCALE, H * SVG_SCALE
    return p


def _assign_desc(structs, assign):
    out = []
    for s, f in assign.items():
        S = structs.get(s)
        if S is not None and len(S["frames"]) > 1:
            out.append("%s → %s" % (S["name"], S["frames"][f]["name"]))
    return out


def _views(bd, hl, items):
    if bd is None:
        return [], {}
    flat = R.flatten_stacked(bd)
    structs = R.index_structures(flat)
    views = R.plan_views(structs, items, max_views=MAX_VIEWS) if items else []
    return [(flat, structs, a, nums) for a, nums in views] or [(flat, structs, {}, [])], structs


def render_pair(old_bd, old_hl, old_items, new_bd, new_hl, new_items, out_prefix, names=None,
                old_title="Previous version", new_title="New version"):
    """Draws the previous and new version in the same SVG.

    *_hl    : {uid: (status, number)}
    *_items : [(number, reveal)]  (for choosing case frames)
    Returns: [{"file", "nums", "desc"}]  — several views if the changes are in different case frames.
    """
    ov, _ = _views(old_bd, old_hl, old_items)
    nv, _ = _views(new_bd, new_hl, new_items)
    n = max(len(ov), len(nv), 1)
    out = []
    for i in range(n):
        sides = []
        desc = []
        nums = set()
        for title, views, hl, tag in ((old_title, ov, old_hl, "old"), (new_title, nv, new_hl, "new")):
            if not views:
                sides.append((title, None, []))
                continue
            flat, structs, assign, vnums = views[i] if i < len(views) else views[0]
            if i >= len(views):
                vnums = []
            vh = {u: h for u, h in hl.items() if h[1] in set(vnums)} if vnums else ({} if i >= len(views) else hl)
            nums |= set(vnums)
            p = _side_painter(flat, assign, vh, names)
            ad = _assign_desc(structs, assign)
            if ad:
                desc.append((tag, ", ".join(ad)))
            sides.append((title + ((" — " + ", ".join(ad)) if ad else ""), p, _paint_side(p)))
        fn = "%s_v%d.svg" % (out_prefix, i + 1)
        _write_svg(fn, sides)
        if len(desc) == 2 and desc[0][1] == desc[1][1]:
            desc = [desc[0][1]]
        else:
            desc = ["%s: %s" % ("previous" if t == "old" else "new", a) for t, a in desc]
        out.append({"file": fn, "nums": sorted(nums), "desc": desc})
    return out


def _write_svg(fn, sides):
    # layout: wide diagrams one above the other, narrow ones side by side
    dims = [(p.W, p.H) if p else (480.0, 160.0) for _, p, _ in sides]
    mw = max(w for w, _ in dims)
    mh = max(h for _, h in dims)
    vertical = mw > mh * 1.3
    hd0 = HEADER * _boost(mw if vertical else sum(w for w, _ in dims) + GAP)
    boxes = []
    if vertical:
        y = 0
        for w, h in dims:
            boxes.append((0, y, w, h))
            y += hd0 + h + GAP
        TW, TH = mw, y - GAP
    else:
        x = 0
        for w, h in dims:
            boxes.append((x, 0, w, h))
            x += w + GAP
        TW, TH = x - GAP, hd0 + mh
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %s %s" font-family="%s">' % (
                 math.ceil(TW), math.ceil(TH), _n(TW), _n(TH), FONT_FAMILY),
             '<defs><pattern id="g" width="%s" height="%s" patternUnits="userSpaceOnUse">'
             '<path d="M %s 0 L 0 0 0 %s" fill="none" stroke="#f0f2f5" stroke-width="1"/></pattern></defs>' % (
                 _n(16 * SVG_SCALE), _n(16 * SVG_SCALE), _n(16 * SVG_SCALE), _n(16 * SVG_SCALE)),
             '<rect width="100%" height="100%" fill="#ffffff"/>']
    hd = hd0
    k = hd0 / HEADER
    for (title, p, body), (bx, by, w, h) in zip(sides, boxes):
        new = title.startswith("New")
        col = "#1a7f37" if new else "#57606a"
        parts.append('<g transform="translate(%s,%s)">' % (_n(bx), _n(by)))
        parts.append('<rect x="0" y="0" width="%s" height="%s" rx="6" fill="%s"/>' % (_n(w), _n(hd - 8 * k), col))
        parts.append('<text x="%s" y="%s" font-size="%s" font-weight="bold" fill="#fff">%s</text>' % (
            _n(14 * k), _n(hd - 16 * k), _n(20 * k), html.escape(title, quote=False)))
        parts.append('<g transform="translate(0,%s)">' % _n(hd))
        parts.append('<rect width="%s" height="%s" fill="url(#g)" stroke="#d0d7de" stroke-width="1"/>' % (_n(w), _n(h)))
        if p is None:
            parts.append('<text x="%s" y="%s" font-size="18" fill="#8c959f" text-anchor="middle">(not in this version)</text>' % (
                _n(w / 2), _n(h / 2)))
        else:
            parts.append('<svg width="%s" height="%s" overflow="hidden">' % (_n(w), _n(h)))
            parts.extend(body)
            parts.append('</svg>')
        parts.append('</g></g>')
    parts.append('</svg>')
    with open(fn, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
