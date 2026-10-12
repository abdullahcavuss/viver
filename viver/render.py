"""
Shared block diagram drawing code (used by svgrender for the CHANGELOG images).

- Changed items are highlighted with colored, numbered boxes; unchanged items are dimmed.
- Stacked Sequences are unfolded like Flat Sequences.
- If changes are in different case frames, as many "views" as needed are planned.
"""
import copy
import os

from PIL import ImageFont

# ---------------------------------------------------------------- settings
OUTLIER_EXTENT = 12000     # if the diagram is wider/taller than this, isolated far-away items are ignored for bounds
OUTLIER_PAD = 2000
MARGIN = 30

WIRE = {"float": (255, 127, 0), "int": (31, 95, 214), "bool": (0, 138, 46), "string": (224, 64, 176),
        "path": (46, 139, 139), "cluster": (139, 90, 43), "refnum": (13, 107, 96), "variant": (123, 46, 138),
        "array": (68, 68, 68), "other": (59, 59, 59), "void": (154, 160, 166)}
ST = {"added": (26, 127, 55), "removed": (207, 34, 46), "modified": (217, 119, 6), "moved": (47, 111, 235),
      "reframed": (130, 80, 223)}
STRUCT = {"forLoop": (143, 148, 155), "whileLoop": (143, 148, 155), "timeLoop": (125, 138, 160),
          "select": (157, 161, 167), "eventStruct": (157, 161, 167), "commentNode": (110, 115, 120),
          "decomposeRecomposeStructure": (108, 141, 179)}
SEQ = (194, 167, 116)

_FONT_CACHE = {}
_FONT_FILES = {
    False: [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf"],
    True: [r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf",
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf"],
}


def font(px, bold=False):
    px = max(6, int(round(px)))
    key = (px, bold)
    if key not in _FONT_CACHE:
        f = None
        for path in _FONT_FILES[bold]:
            if os.path.exists(path):
                try:
                    f = ImageFont.truetype(path, px)
                    break
                except OSError:
                    continue
        if f is None:
            try:
                f = ImageFont.load_default(size=px)
            except TypeError:
                f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return _FONT_CACHE[key]


# ---------------------------------------------------------------- Stacked -> Flat
def _shift_node(n, dx):
    n["x"] += dx
    if n.get("inner"):
        n["inner"]["x"] += dx
    for t in n.get("terms", []):
        t["x"] += dx
    for t in n.get("iterms", []) or []:
        t["x"] += dx
    for f in n.get("frames", []):
        _shift_diagram(f["diagram"], dx)


def _shift_diagram(d, dx):
    for n in d["nodes"]:
        _shift_node(n, dx)
    for t in d["notes"]:
        t["x"] += dx
    for w in d["wires"]:
        for e in w["ends"]:
            e["x"] += dx


def _contains(n, target):
    for f in n.get("frames", []):
        for c in f["diagram"]["nodes"]:
            if c is target or _contains(c, target):
                return True
    return False


def _key(x, y):
    return "%d|%d" % (round(x), round(y))


def flatten_stacked(bd):
    """Unfolds Stacked Sequence frames side by side (display only; works on a copy)."""
    bd = copy.deepcopy(bd)
    seqs = []

    def collect(d, depth):
        for n in d["nodes"]:
            for f in n.get("frames", []):
                collect(f["diagram"], depth + 1)
            if n["cls"] == "sequence" and len(n.get("frames", [])) > 1:
                seqs.append((depth, n))

    collect(bd, 0)
    seqs.sort(key=lambda x: -x[0])
    for _, S in seqs:
        k = len(S["frames"])
        inner = S.get("inner") or {"x": S["x"] + 6, "y": S["y"] + 6, "w": S["w"] - 12, "h": S["h"] - 12}
        bL = max(3, inner["x"] - S["x"])
        step = S["w"] - bL
        delta = (k - 1) * step
        right = S["x"] + S["w"]
        right_terms = set()
        for t in S.get("terms", []):
            if t["x"] >= right - bL - 2:
                right_terms.add(_key(t["x"], t["y"]))
                t["x"] += delta
        for i, f in enumerate(S["frames"]):
            if i:
                _shift_diagram(f["diagram"], i * step)

        def walk(d):
            for n in d["nodes"]:
                if n is S:
                    continue
                if n["x"] >= right:
                    _shift_node(n, delta)
                    continue
                if _contains(n, S):
                    n["w"] += delta
                    if n.get("inner"):
                        n["inner"]["w"] += delta
                    for t in n.get("terms", []):
                        if t["x"] >= right:
                            t["x"] += delta
                    for t in n.get("iterms", []) or []:
                        if t["x"] >= right:
                            t["x"] += delta
                for f in n.get("frames", []):
                    walk(f["diagram"])
            for t in d["notes"]:
                if t["x"] >= right:
                    t["x"] += delta
            for w in d["wires"]:
                for e in w["ends"]:
                    if e["x"] >= right or _key(e["x"], e["y"]) in right_terms:
                        e["x"] += delta

        walk(bd)
        for i, f in enumerate(S["frames"]):
            for w in f["diagram"]["wires"]:
                for e in w["ends"]:
                    if _key(e["x"] - i * step, e["y"]) in right_terms:
                        e["x"] += delta - i * step
        S["w"] += delta
        if S.get("inner"):
            S["inner"]["w"] += delta
        S["flat"] = {"step": step, "bL": bL, "k": k}
    return bd


def diagram_bounds(bd):
    xs, ys, xe, ye = [], [], [], []

    def b(d):
        for n in d["nodes"]:
            xs.append(n["x"]); ys.append(n["y"]); xe.append(n["x"] + n["w"]); ye.append(n["y"] + n["h"])
            for f in n.get("frames", []):
                b(f["diagram"])
        for t in d["notes"]:
            xs.append(t["x"]); ys.append(t["y"]); xe.append(t["x"] + t["w"]); ye.append(t["y"] + t["h"])
        for w in d["wires"]:
            for e in w["ends"]:
                xs.append(e["x"]); ys.append(e["y"]); xe.append(e["x"]); ye.append(e["y"])

    b(bd)
    if not xs:
        return [0, 0, 400, 300]
    return [min(xs), min(ys), max(xe), max(ye)]


def robust_bounds(bd):
    """Like diagram_bounds, but ignores a few items lying very far from the diagram
    (broken/hidden objects) when they would make the bounds huge."""
    b = diagram_bounds(bd)
    if b[2] - b[0] <= OUTLIER_EXTENT and b[3] - b[1] <= OUTLIER_EXTENT:
        return b
    xs, ys = [], []

    def w(d):
        for n in d["nodes"]:
            xs.append(n["x"] + n["w"] / 2.0); ys.append(n["y"] + n["h"] / 2.0)
            for f in n.get("frames", []):
                w(f["diagram"])
        for t in d["notes"]:
            xs.append(t["x"]); ys.append(t["y"])

    w(bd)
    if len(xs) < 3:
        return b
    xs.sort(); ys.sort()
    q = lambda a, p: a[min(len(a) - 1, max(0, int(round(p * (len(a) - 1)))))]
    lo_x, hi_x = q(xs, 0.02) - OUTLIER_PAD, q(xs, 0.98) + OUTLIER_PAD
    lo_y, hi_y = q(ys, 0.02) - OUTLIER_PAD, q(ys, 0.98) + OUTLIER_PAD
    # real edges of the items that fall inside the range
    X0, Y0, X1, Y1 = [], [], [], []

    def k(d):
        for n in d["nodes"]:
            cx, cy = n["x"] + n["w"] / 2.0, n["y"] + n["h"] / 2.0
            if lo_x <= cx <= hi_x and lo_y <= cy <= hi_y:
                X0.append(n["x"]); Y0.append(n["y"]); X1.append(n["x"] + n["w"]); Y1.append(n["y"] + n["h"])
            for f in n.get("frames", []):
                k(f["diagram"])
        for t in d["notes"]:
            if lo_x <= t["x"] <= hi_x and lo_y <= t["y"] <= hi_y:
                X0.append(t["x"]); Y0.append(t["y"]); X1.append(t["x"] + t["w"]); Y1.append(t["y"] + t["h"])

    k(bd)
    if not X0:
        return b
    return [min(X0), min(Y0), max(X1), max(Y1)]


def index_structures(bd):
    out = {}

    def w(d):
        for n in d["nodes"]:
            if n.get("frames"):
                out[n["uid"]] = n
                for f in n["frames"]:
                    w(f["diagram"])

    w(bd)
    return out


def default_frame(S):
    best, bc = None, 0
    for i, f in enumerate(S["frames"]):
        if (f.get("changes") or 0) > bc:
            best, bc = i, f["changes"]
    return best if best is not None else (S.get("active") or 0)


# ---------------------------------------------------------------- views (case frames)
def plan_views(structs, items, max_views=8):
    """items: [(num, reveal)] -> [(assign{struct uid: frame}, [num,...])]"""

    def chosen(assign, s):
        S = structs.get(s)
        if S is None or S.get("flat"):
            return None
        return assign.get(s, default_frame(S))

    def visible(assign, reveal):
        for s, f in reveal or []:
            c = chosen(assign, s)
            if c is not None and c != f:
                return False
        return True

    remaining = list(items)
    views = []
    while remaining and len(views) < max_views:
        assign = {}
        for num, reveal in remaining:
            if all(assign.get(s, f) == f for s, f in reveal or [] if s in structs and not structs[s].get("flat")):
                for s, f in reveal or []:
                    if s in structs and not structs[s].get("flat"):
                        assign[s] = f
        covered = [num for num, reveal in remaining if visible(assign, reveal)]
        if not covered:
            break
        views.append((assign, covered))
        remaining = [it for it in remaining if it[0] not in covered]
    return views


# ---------------------------------------------------------------- drawing
class Painter:
    def __init__(self, bd, assign, highlights, origin, scale, size_px, prim_names):
        self.bd = bd
        self.assign = assign
        self.hl = highlights          # uid -> (status, number)
        self.ox, self.oy = origin
        self.s = scale
        self.W, self.H = max(1, int(size_px[0])), max(1, int(size_px[1]))
        self.prim = prim_names or {}
        self.structs = index_structures(bd)

    def P(self, x, y):
        return ((x - self.ox) * self.s, (y - self.oy) * self.s)

    def R(self, x, y, w, h):
        x0, y0 = self.P(x, y)
        return [x0, y0, x0 + w * self.s, y0 + h * self.s]

    def lw(self, units):
        return max(1, int(round(units * self.s)))

    def frame_of(self, S):
        if S.get("flat"):
            return None
        return self.assign.get(S["uid"], default_frame(S))

    # -------- text
    def text_in(self, d, box, text, size_u, color=(28, 33, 40), bold=False, max_lines=4, align="center"):
        f = font(size_u * self.s, bold)
        x0, y0, x1, y1 = box
        width = x1 - x0 - 2 * self.s
        lines = []
        for para in str(text).replace("\r", "").split("\n"):
            words = para.replace("_", "_ ").split(" ")
            cur = ""
            for wd in words:
                cand = (cur + " " + wd).strip() if cur else wd
                if d.textlength(cand.replace("_ ", "_"), font=f) <= width or not cur:
                    cur = cand
                else:
                    lines.append(cur)
                    cur = wd
            lines.append(cur)
            if len(lines) > max_lines:
                break
        lines = [l.replace("_ ", "_") for l in lines]
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1][:-1] + "…"
        fitted = []
        for l in lines:
            while d.textlength(l, font=f) > width and len(l) > 2:
                l = l[:-2] + "…"
            fitted.append(l)
        lh = size_u * self.s * 1.15
        total = lh * len(fitted)
        y = (y0 + y1) / 2 - total / 2
        for l in fitted:
            tw = d.textlength(l, font=f)
            x = (x0 + x1) / 2 - tw / 2 if align == "center" else x0 + 2 * self.s
            d.text((x, y), l, font=f, fill=color)
            y += lh

    # -------- items
    def wire_path(self, w):
        ends = sorted(w["ends"], key=lambda e: e["x"])
        s = ends[0]
        segs = []
        for t in ends[1:]:
            if abs(t["y"] - s["y"]) < 1:
                segs.append([(s["x"], s["y"]), (t["x"], t["y"])])
                continue
            mx = (s["x"] + t["x"]) / 2
            if t["x"] - s["x"] < 12:
                mx = s["x"] + 6
            segs.append([(s["x"], s["y"]), (mx, s["y"]), (mx, t["y"]), (t["x"], t["y"])])
        return segs

    def draw_wire(self, d, w):
        col = WIRE.get(w.get("cat"), WIRE["other"])
        for seg in self.wire_path(w):
            pts = [self.P(*p) for p in seg]
            if w.get("arr"):
                d.line(pts, fill=col, width=self.lw(3.2), joint="curve")
                d.line(pts, fill=(255, 255, 255), width=max(1, self.lw(1)))
            elif w.get("cat") == "cluster":
                d.line(pts, fill=col, width=self.lw(2.6), joint="curve")
            else:
                d.line(pts, fill=col, width=self.lw(1.6 if w.get("cat") != "void" else 1), joint="curve")

    def draw_structure(self, d, n):
        cls = n["cls"]
        seq = "equence" in cls or cls == "sequenceFrame"
        outer = self.R(n["x"], n["y"], n["w"], n["h"])
        d.rectangle(outer, fill=SEQ if seq else STRUCT.get(cls, (157, 161, 167)), outline=(107, 112, 118), width=self.lw(0.6))
        inner = n.get("inner") or {"x": n["x"] + 5, "y": n["y"] + 5, "w": n["w"] - 10, "h": n["h"] - 10}
        if n.get("flat"):
            st, bL = n["flat"]["step"], n["flat"]["bL"]
            for i in range(n["flat"]["k"]):
                fx = n["x"] + i * st + bL
                fw = st - bL if i < n["flat"]["k"] - 1 else (n["x"] + n["w"] - bL) - fx
                d.rectangle(self.R(fx, inner["y"], fw, inner["h"]), fill=(255, 255, 255), outline=(125, 130, 136), width=1)
                b = self.R(fx + 3, inner["y"] + 2, 15, 11)
                chg = (n["frames"][i].get("changes") or 0) > 0
                d.rectangle(b, fill=(255, 244, 224) if chg else (244, 241, 232), outline=(217, 119, 6) if chg else (179, 163, 122))
                self.text_in(d, b, str(i), 8, (180, 83, 9) if chg else (122, 106, 58), bold=True, max_lines=1)
        else:
            d.rectangle(self.R(inner["x"], inner["y"], inner["w"], inner["h"]),
                        fill=(242, 242, 242) if cls == "commentNode" else (255, 255, 255), outline=(125, 130, 136), width=1)

    def draw_selector(self, d, n):
        frames = n.get("frames", [])
        if len(frames) < 2 or n.get("flat"):
            return
        idx = self.frame_of(n)
        name = str(frames[idx]["name"])
        w = min(max(60, len(name) * 5.8 + 40), n["w"] - 20)
        x = n["x"] + (n["w"] - w) / 2
        y = n["y"] - 1
        d.rectangle(self.R(x, y, w, 15), fill=(245, 246, 247), outline=(107, 112, 118), width=1)
        for ax, pts in ((x, [(x + 9, y + 4), (x + 4, y + 7.5), (x + 9, y + 11)]),
                        (x + w - 13, [(x + w - 9, y + 4), (x + w - 4, y + 7.5), (x + w - 9, y + 11)])):
            d.rectangle(self.R(ax, y, 13, 15), fill=(221, 224, 228), outline=(107, 112, 118), width=1)
            d.polygon([self.P(*p) for p in pts], fill=(51, 51, 51))
        chg = (frames[idx].get("changes") or 0) > 0
        self.text_in(d, self.R(x + 13, y, w - 26, 15), name, 9.5, (180, 83, 9) if chg else (28, 33, 40), bold=True, max_lines=1)

    def draw_struct_terms(self, d, n):
        for t in n.get("terms", []):
            if t.get("w") is None:
                continue
            box = self.R(t["x"] - t["w"] / 2, t["y"] - t["h"] / 2, t["w"], t["h"])
            cls = t.get("cls")
            if cls == "lMax":
                d.rectangle(box, fill=(255, 255, 255), outline=(31, 95, 214), width=1)
                self.text_in(d, box, "N", 11, (31, 95, 214), bold=True, max_lines=1)
            elif cls in ("lSR", "rSR"):
                d.rectangle(box, fill=(255, 255, 255), outline=(90, 90, 90), width=self.lw(1.2))
            elif cls == "caseSel":
                d.rectangle(box, fill=(255, 255, 255), outline=(90, 90, 90), width=1)
                self.text_in(d, box, "?", 9, (60, 60, 60), bold=True, max_lines=1)
            else:
                d.rectangle(box, fill=(110, 110, 110), outline=(80, 80, 80), width=1)
        for t in n.get("iterms", []) or []:
            box = self.R(t["x"], t["y"], t["w"], t["h"])
            if t["cls"] == "lCnt":
                d.rectangle(box, fill=(255, 255, 255), outline=(31, 95, 214), width=1)
                self.text_in(d, box, "i", 11, (31, 95, 214), bold=True, max_lines=1)
            elif t["cls"] == "lTst":
                d.rectangle(box, fill=(255, 255, 255), outline=(0, 138, 46), width=1)
                cx, cy = self.P(t["x"] + t["w"] / 2, t["y"] + t["h"] / 2)
                r = t["w"] * self.s * 0.36
                d.regular_polygon((cx, cy, r), 8, rotation=22.5, fill=(209, 36, 47))

    def node_name(self, n):
        if n["kind"] == "prim":
            return self.prim.get(str(n.get("prim")), "#%s" % n.get("prim"))
        return n["name"]

    def draw_node(self, d, n):
        box = self.R(n["x"], n["y"], n["w"], n["h"])
        name = self.node_name(n)
        k = n["kind"]
        if k == "subvi":
            d.rectangle(box, fill=(255, 254, 245), outline=(61, 61, 61), width=1)
            d.rectangle(self.R(n["x"], n["y"], n["w"], 5), fill=(47, 93, 138))
            self.text_in(d, self.R(n["x"], n["y"] + 5, n["w"], n["h"] - 5), name.replace(".vi", ""), 5.8, bold=True, max_lines=4)
        elif k == "prim":
            named = str(n.get("prim")) in self.prim
            d.rectangle(box, fill=(255, 247, 204) if named else (255, 251, 230), outline=(122, 106, 42), width=1)
            self.text_in(d, box, name, 6 if named else 6.5, (28, 33, 40) if named else (138, 122, 58), bold=True, max_lines=3)
        elif k == "const":
            col = WIRE.get(n.get("cat"), WIRE["other"])
            d.rectangle(box, fill=(255, 255, 255), outline=col, width=self.lw(1.2))
            v = n.get("value")
            txt = str(v) if v is not None else name
            if txt.startswith('"') and txt.endswith('"') and len(txt) > 1:
                txt = txt[1:-1]
            if n["h"] >= 9:
                self.text_in(d, [box[0] + 2 * self.s, box[1], box[2], box[3]], txt, 8, col if col != WIRE["other"] else (51, 51, 51),
                             bold=True, max_lines=1, align="left")
        elif k == "variable":
            w = self.term_wire.get(n["uid"])
            col = WIRE.get(w.get("cat"), WIRE["other"]) if w else (85, 85, 85)
            d.rectangle(box, fill=(255, 255, 255), outline=col, width=self.lw(2.2 if n.get("read") else 1.1))
            mx = n["x"] + n["w"] - 13 if n.get("read") else n["x"] + 1.5
            mb = self.R(mx, n["y"] + 2, 11.5, n["h"] - 4)
            d.rectangle(mb, fill=(106, 61, 154) if not n.get("local") else (47, 111, 235))
            self.text_in(d, mb, "L" if n.get("local") else "G", 8, (255, 255, 255), bold=True, max_lines=1)
            tb = self.R(n["x"] + (2 if n.get("read") else 14), n["y"], n["w"] - 16, n["h"])
            self.text_in(d, tb, name, 8.5, bold=True, max_lines=1, align="left")
        elif k == "terminal":
            w = self.term_wire.get(n["uid"])
            col = WIRE.get(w.get("cat"), WIRE["other"]) if w else (85, 85, 85)
            ctl = n.get("io") == "control"
            d.rectangle(box, fill=(255, 255, 255), outline=col, width=self.lw(2.2 if ctl else 1.1))
            self.text_in(d, box, (n.get("ctype_name") or "")[:4].upper(), 7, col, bold=True, max_lines=1)
            f = font(8.5 * self.s)
            tx = box[2] - d.textlength(name, font=f) if ctl else box[0]
            d.text((tx, box[1] - 11 * self.s), name.replace("\n", " "), font=f, fill=(28, 33, 40))
        else:
            d.rectangle(box, fill=(244, 245, 247), outline=(91, 97, 104), width=1)
            if n["w"] >= 20 and n["h"] >= 12:
                self.text_in(d, box, name, 5.8, (59, 65, 72), bold=True, max_lines=max(1, int(n["h"] // 7)))

    def draw_note(self, d, t):
        box = self.R(t["x"], t["y"], max(t["w"], 20), max(t["h"], 14))
        d.rectangle(box, fill=(255, 255, 215), outline=(227, 221, 155), width=1)
        lines = max(1, int(max(t["h"], 14) / 12.5))
        self.text_in(d, box, t["text"], 10.5, (28, 33, 40), max_lines=lines, align="left")

    # -------- traversal
    def visible_items(self):
        nodes, wires, notes, structs = [], [], [], []

        def walk(d):
            for w in d["wires"]:
                wires.append(w)
            for t in d["notes"]:
                notes.append(t)
            for n in d["nodes"]:
                if n.get("frames"):
                    structs.append(n)
                    if n.get("flat"):
                        for f in n["frames"]:
                            walk(f["diagram"])
                    else:
                        walk(n["frames"][self.frame_of(n)]["diagram"])
                else:
                    nodes.append(n)

        walk(self.bd)
        return nodes, wires, notes, structs
