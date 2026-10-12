"""
Compares two VI models.

Matching order:
  1. Same uid + same signature (LabVIEW largely preserves object uids between saves)
  2. Same signature in the same parent diagram, nearest position
  3. Same class in the same parent diagram, nearby position  -> "replaced" (e.g. a subVI replaced by another VI)

Every node and wire gets a status:  same | added | removed | modified | moved
"""
import statistics

MOVE_TOLERANCE = 3  # pixels


# Nodes whose settings LabVIEW regenerates without the user touching them (especially on FPGA).
# If only their internal setting changed (wiring/terminals/name unchanged), the change goes into the "auto" category.
AUTO_CLASSES = {"timeLoop", "timeLoopExtNode", "timeDataNode", "timeFlatSequence", "timeSequence",
                "xNode", "xStructure"}
AUTO_LABEL = "Timed Loop / XNode internal setting"


def change_cat(ch):
    return ch.get("cat") or CHANGE_CATEGORY.get(ch["kind"], "logic")


def _walk(diagram, path, reveal, out_nodes, out_diagrams, parent_key):
    """Flatten the diagram tree; record each node's path and the frames needed to make it visible."""
    out_diagrams[parent_key] = diagram
    for n in diagram["nodes"]:
        n["_path"] = list(path)
        n["_reveal"] = list(reveal)
        n["_parent"] = parent_key
        out_nodes[n["uid"]] = n
        for fi, f in enumerate(n.get("frames", [])):
            label = n["name"] + (" [%s]" % f["name"] if len(n.get("frames", [])) > 1 else "")
            _walk(f["diagram"], path + [label], reveal + [[n["uid"], fi]], out_nodes, out_diagrams,
                  "%s#%d" % (n["uid"], fi))
    for w in diagram["wires"]:
        w["_parent"] = parent_key
        w["_path"] = list(path)
        w["_reveal"] = list(reveal)
    for t in diagram["notes"]:
        t["_parent"] = parent_key
        t["_path"] = list(path)
        t["_reveal"] = list(reveal)


def _all_wires(diagram):
    for w in diagram["wires"]:
        yield w
    for n in diagram["nodes"]:
        for f in n.get("frames", []):
            yield from _all_wires(f["diagram"])


def _all_notes(diagram):
    for t in diagram["notes"]:
        yield t
    for n in diagram["nodes"]:
        for f in n.get("frames", []):
            yield from _all_notes(f["diagram"])


def _dist(a, b, dx=0, dy=0):
    return abs(a["x"] + dx - b["x"]) + abs(a["y"] + dy - b["y"])


class Differ:
    def __init__(self, old, new):
        self.old = old
        self.new = new
        self.on, self.od = {}, {}
        self.nn, self.nd = {}, {}
        self.pairs = {}      # old uid -> new uid
        self.rpairs = {}     # new uid -> old uid
        self.changes = []
        self.dx = self.dy = 0

    # ------------------------------------------------------------------
    def run(self):
        ob = self.old.get("bd") or {"uid": "root", "nodes": [], "wires": [], "notes": []}
        nb = self.new.get("bd") or {"uid": "root", "nodes": [], "wires": [], "notes": []}
        _walk(ob, [], [], self.on, self.od, "root")
        _walk(nb, [], [], self.nn, self.nd, "root")

        self._match_by_uid()
        self._compute_shift()
        self._match_rest()
        self._classify_nodes()
        self._diff_wires(ob, nb)
        self._diff_notes(ob, nb)
        self._mark_frames(ob)
        self._mark_frames(nb)
        return self._result()

    def _pair(self, ou, nu):
        self.pairs[ou] = nu
        self.rpairs[nu] = ou

    def _parent_pair(self, old_parent):
        """New-side counterpart of an old parent diagram key."""
        if old_parent == "root":
            return "root"
        suid, fi = old_parent.split("#")
        nu = self.pairs.get(suid)
        if nu is None:
            return None
        old_node = self.on[suid]
        new_node = self.nn[nu]
        ofr = old_node.get("frames", [])
        nfr = new_node.get("frames", [])
        fi = int(fi)
        if fi < len(ofr):
            # first by frame name, then by order
            name = ofr[fi]["name"]
            for j, f in enumerate(nfr):
                if f["name"] == name:
                    return "%s#%d" % (nu, j)
        if fi < len(nfr):
            return "%s#%d" % (nu, fi)
        return None

    def _match_by_uid(self):
        for uid, o in self.on.items():
            n = self.nn.get(uid)
            if n is not None and n["sig"] == o["sig"] and n["cls"] == o["cls"]:
                self._pair(uid, uid)

    def _compute_shift(self):
        dxs, dys = [], []
        for ou, nu in self.pairs.items():
            o, n = self.on[ou], self.nn[nu]
            if o["_parent"] == "root" and n["_parent"] == "root":
                dxs.append(n["x"] - o["x"])
                dys.append(n["y"] - o["y"])
        if len(dxs) >= 3:
            # if most items moved by the same amount on each axis, it is a global shift (Clean Up, margins, etc.)
            for axis, vals in (("dx", dxs), ("dy", dys)):
                m = statistics.median(vals)
                if sum(1 for v in vals if abs(v - m) <= 1) >= len(vals) * 0.6:
                    setattr(self, axis, m)

    def _match_rest(self):
        # several passes: once structures are matched, their contents can match too
        for _ in range(4):
            changed = False
            for strict in (True, False):
                groups = {}
                for ou, o in self.on.items():
                    if ou in self.pairs:
                        continue
                    np = self._parent_pair(o["_parent"])
                    if np is None:
                        continue
                    groups.setdefault(np, []).append(o)
                for np, olds in groups.items():
                    cands = [n for nu, n in self.nn.items() if nu not in self.rpairs and n["_parent"] == np]
                    if not cands:
                        continue
                    triples = []
                    for o in olds:
                        for n in cands:
                            if strict and n["sig"] != o["sig"]:
                                continue
                            if not strict:
                                if n["cls"] != o["cls"]:
                                    continue
                                if _dist(o, n, self.dx, self.dy) > 60:
                                    continue
                            triples.append((_dist(o, n, self.dx, self.dy), o["uid"], n["uid"]))
                    triples.sort()
                    for d, ou, nu in triples:
                        if ou in self.pairs or nu in self.rpairs:
                            continue
                        self._pair(ou, nu)
                        changed = True
            if not changed:
                break

    # ------------------------------------------------------------------
    def _add_change(self, kind, node=None, old=None, new=None, title="", detail="", category="node"):
        ref = new if new is not None else old
        ch = {
            "id": len(self.changes),
            "kind": kind,
            "category": category,
            "title": title,
            "detail": detail,
            "path": " › ".join(ref.get("_path", [])) if ref else "",
            "old_uid": old["uid"] if old is not None else None,
            "new_uid": new["uid"] if new is not None else None,
            "old_reveal": old.get("_reveal") if old is not None else None,
            "new_reveal": new.get("_reveal") if new is not None else None,
        }
        self.changes.append(ch)
        for x in (old, new):
            if x is not None:
                x.setdefault("chg", []).append(ch["id"])
        return ch

    def _label(self, n):
        if n["kind"] == "variable":
            return "%s variable “%s”%s" % ("Local" if n.get("local") else "Global", n["name"],
                                        "" if n.get("local") else " (%s)" % n.get("owner", ""))
        kinds = {"subvi": "SubVI", "prim": "Primitive", "structure": "Structure", "const": "Constant",
                 "terminal": "Terminal", "node": "Node"}
        return "%s “%s”" % (kinds.get(n["kind"], "Node"), n["name"])

    def _expected_shift(self, o, n):
        """How much an item is expected to move together with its enclosing structure (not 'moved' if its relative position is unchanged)."""
        po = o.get("_parent", "root")
        if po == "root":
            return self.dx, self.dy
        suid = po.split("#")[0]
        nu = self.pairs.get(suid)
        if nu is None:
            return self.dx, self.dy
        so, sn = self.on[suid], self.nn[nu]
        return sn["x"] - so["x"], sn["y"] - so["y"]

    def _classify_nodes(self):
        pid = 0
        for ou, nu in self.pairs.items():
            pid += 1
            o, n = self.on[ou], self.nn[nu]
            o["pid"] = n["pid"] = "p%d" % pid
            reasons = []
            if o["kind"] == "const" and (o.get("raw") or "") != (n.get("raw") or ""):
                ov, nv = o.get("value"), n.get("value")
                if ov == nv:  # same display text but different raw data (e.g. cluster contents)
                    reasons.append("value changed")
                else:
                    reasons.append("value %s → %s" % (ov, nv))
            elif o["kind"] == "variable" and (o["name"] != n["name"] or o.get("owner") != n.get("owner")):
                reasons.append("“%s” → “%s”%s" % (o["name"], n["name"],
                               "" if o.get("owner") == n.get("owner") else " (%s → %s)" % (o.get("owner"), n.get("owner"))))
            elif o["name"] != n["name"]:
                reasons.append("“%s” → “%s”" % (o["name"], n["name"]))
            if len(o["terms"]) != len(n["terms"]):
                reasons.append("terminal count %d → %d" % (len(o["terms"]), len(n["terms"])))
            ofr = [f["name"] for f in o.get("frames", [])]
            nfr = [f["name"] for f in n.get("frames", [])]
            if ofr != nfr:
                added = [f for f in nfr if f not in ofr]
                removed = [f for f in ofr if f not in nfr]
                parts = []
                if added:
                    parts.append("frame added: " + ", ".join(added))
                if removed:
                    parts.append("frame removed: " + ", ".join(removed))
                if not parts:
                    parts.append("frame order changed")
                reasons.append("; ".join(parts))
            if o.get("ctype") != n.get("ctype") and (o.get("ctype") or n.get("ctype")):
                reasons.append("type %s → %s" % (o.get("ctype"), n.get("ctype")))
            if not reasons and o.get("chash") and n.get("chash") and o["chash"] != n["chash"] and o["kind"] not in ("const", "terminal"):
                reasons.append("setting/property changed")
            if o["kind"] == "variable" and o.get("read") != n.get("read"):
                reasons.append("read → write" if o.get("read") else "write → read")
            if o.get("io") != n.get("io") and o["kind"] == "terminal":
                reasons.append("%s → %s" % (o.get("io"), n.get("io")))
            ex, ey = self._expected_shift(o, n)
            moved = abs(o["x"] + ex - n["x"]) > MOVE_TOLERANCE or abs(o["y"] + ey - n["y"]) > MOVE_TOLERANCE
            resized = (o["w"] != n["w"] or o["h"] != n["h"]) and o["kind"] == "structure"
            if o["_parent"] != "root" and self._parent_pair(o["_parent"]) != n["_parent"]:
                reasons.append("moved to another structure/frame")
            if reasons:
                reframe = reasons == ["moved to another structure/frame"]
                o["st"] = n["st"] = "reframed" if reframe else "modified"
                auto = reasons == ["setting/property changed"] and (o.get("cls") in AUTO_CLASSES or n.get("cls") in AUTO_CLASSES)
                ch = self._add_change("modified", old=o, new=n,
                                      title=self._label(n) + (" moved into another structure/frame" if reframe else " modified"),
                                      detail="" if reframe else
                                      "internal setting changed (may have been regenerated by LabVIEW)" if auto else "; ".join(reasons))
                if reframe:
                    ch["reframe"] = True
                if auto:
                    ch["cat"] = "auto"
                    o["auto"] = n["auto"] = True
            elif moved or resized:
                o["st"] = n["st"] = "moved"
                self._add_change("moved", old=o, new=n, title=self._label(n) + (" resized" if resized and not moved else " moved"),
                                 detail="position (%d,%d) → (%d,%d)" % (o["x"], o["y"], n["x"], n["y"]),
                                 category="cosmetic")
            else:
                o["st"] = n["st"] = "same"
        for ou, o in self.on.items():
            if ou not in self.pairs:
                o["st"] = "removed"
                self._add_change("removed", old=o, title=self._label(o) + " removed")
        for nu, n in self.nn.items():
            if nu not in self.rpairs:
                n["st"] = "added"
                self._add_change("added", new=n, title=self._label(n) + " added")

    def _wire_key(self, w, side):
        nodes = self.on if side == "old" else self.nn
        parts = []
        for e in w["ends"]:
            k = e["key"].split(":")
            n = nodes.get(e["node"])
            pid = n.get("pid") if n is not None else None
            if pid is None:
                pid = side + ":" + e["node"]
            parts.append(":".join([pid] + k[1:]))
        return tuple(sorted(parts))

    def _diff_wires(self, ob, nb):
        old_w = {}
        for w in _all_wires(ob):
            old_w.setdefault(self._wire_key(w, "old"), []).append(w)
        new_w = {}
        for w in _all_wires(nb):
            new_w.setdefault(self._wire_key(w, "new"), []).append(w)
        wid = 0
        for k, ws in old_w.items():
            nws = new_w.get(k, [])
            for i, w in enumerate(ws):
                if i < len(nws):
                    wid += 1
                    w["pid"] = nws[i]["pid"] = "w%d" % wid
                    w["st"] = nws[i]["st"] = "same"
                    if w["cat"] != nws[i]["cat"] or w["arr"] != nws[i]["arr"]:
                        w["st"] = nws[i]["st"] = "modified"
                        self._add_change("wire_modified", old=self._wire_ref(w), new=self._wire_ref(nws[i]),
                                         title="Wire data type changed",
                                         detail="%s%s → %s%s" % (w["cat"], "[]" if w["arr"] else "",
                                                                  nws[i]["cat"], "[]" if nws[i]["arr"] else ""),
                                         category="wire")
                else:
                    w["st"] = "removed"
                    self._add_change("wire_removed", old=self._wire_ref(w), title="Wire removed",
                                     detail=self._wire_desc(w, "old"), category="wire")
        for k, ws in new_w.items():
            ows = old_w.get(k, [])
            for i, w in enumerate(ws):
                if i >= len(ows):
                    w["st"] = "added"
                    self._add_change("wire_added", new=self._wire_ref(w), title="Wire added",
                                     detail=self._wire_desc(w, "new"), category="wire")

    def _wire_ref(self, w):
        w.setdefault("uid", "w?")
        return w

    def _wire_desc(self, w, side):
        nodes = self.on if side == "old" else self.nn
        names_ = []
        for e in w["ends"]:
            n = nodes.get(e["node"])
            nm = n["name"] if n else "?"
            if nm not in names_:
                names_.append(nm)
        return " ↔ ".join(names_[:4]) + (" …" if len(names_) > 4 else "")

    def _diff_notes(self, ob, nb):
        on = list(_all_notes(ob))
        nn = list(_all_notes(nb))
        used = set()
        for o in on:
            match = None
            for i, n in enumerate(nn):
                if i in used:
                    continue
                if n["text"] == o["text"]:
                    match = i
                    break
            if match is None:
                o["st"] = "removed"
                self._add_change("note_removed", old=o, title="Comment removed", detail=o["text"][:140], category="note")
            else:
                used.add(match)
                o["st"] = nn[match]["st"] = "same"
        for i, n in enumerate(nn):
            if i not in used:
                n["st"] = "added"
                self._add_change("note_added", new=n, title="Comment added", detail=n["text"][:140], category="note")

    def _mark_frames(self, diagram):
        """Number of changes inside each structure frame (for the badge). Returns: subtotal."""
        total = 0
        for n in diagram["nodes"]:
            if n.get("st") not in (None, "same") and n.get("st") != "moved":
                total += 1
            for f in n.get("frames", []):
                c = self._mark_frames(f["diagram"])
                f["changes"] = c
                total += c
        for w in diagram["wires"]:
            if w.get("st") in ("added", "removed", "modified"):
                total += 1
        for t in diagram["notes"]:
            if t.get("st") in ("added", "removed"):
                total += 1
        return total

    # ------------------------------------------------------------------
    def _result(self):
        res = {
            "changes": self.changes,
            "shift": [self.dx, self.dy],
            "fp": diff_fp(self.old.get("fp", []), self.new.get("fp", [])),
            "deps": diff_deps(self.old.get("deps", []), self.new.get("deps", [])),
            "props": diff_props(self.old.get("meta", {}).get("props", {}), self.new.get("meta", {}).get("props", {})),
        }
        counts = {"added": 0, "removed": 0, "modified": 0, "moved": 0, "wire_added": 0, "wire_removed": 0,
                  "wire_modified": 0, "note_added": 0, "note_removed": 0}
        counts["auto"] = 0
        for c in self.changes:
            if c.get("cat") == "auto":
                counts["auto"] += 1
            else:
                counts[c["kind"]] = counts.get(c["kind"], 0) + 1
        res["counts"] = counts
        res["functional"] = sum(v for k, v in counts.items() if k not in ("moved", "auto")) + \
            len(res["fp"]) + len(res["deps"]) + len(res["props"])
        # drop internal fields such as _path/_reveal (JSON size)
        for coll in (self.on, self.nn):
            for n in coll.values():
                n.pop("_parent", None)
                n["path"] = " › ".join(n.pop("_path", []))
                n.pop("_reveal", None)
        for b in (self.old.get("bd"), self.new.get("bd")):
            if b:
                for w in _all_wires(b):
                    w.pop("_parent", None); w.pop("_path", None); w.pop("_reveal", None)
                for t in _all_notes(b):
                    t.pop("_parent", None); t.pop("_path", None); t.pop("_reveal", None)
        return res


def diff_fp(old, new):
    out = []
    ob = {c["name"]: c for c in old}
    nb = {c["name"]: c for c in new}
    for name, c in ob.items():
        if name not in nb:
            out.append({"kind": "removed", "name": name, "detail": "%s %s removed" % (c.get("ctype_name") or "", _io(c))})
        else:
            n = nb[name]
            diffs = []
            if c.get("ctype") != n.get("ctype"):
                diffs.append("type %s → %s" % (c.get("ctype_name"), n.get("ctype_name")))
            if c.get("io") != n.get("io"):
                diffs.append("%s → %s" % (_io(c), _io(n)))
            if not diffs and c.get("chash") and n.get("chash") and c["chash"] != n["chash"]:
                diffs.append("typedef contents updated" if (c.get("typedef") or n.get("typedef")) else "appearance/property changed")
            if diffs:
                out.append({"kind": "modified", "name": name, "detail": "; ".join(diffs)})
    for name, n in nb.items():
        if name not in ob:
            out.append({"kind": "added", "name": name, "detail": "%s %s added" % (n.get("ctype_name") or "", _io(n))})
    return out


def _io(c):
    return {"control": "control", "indicator": "indicator"}.get(c.get("io"), "")


def diff_deps(old, new):
    out = []
    os_ = {(d["kind"], d["name"]) for d in old}
    ns_ = {(d["kind"], d["name"]) for d in new}
    for k in sorted(os_ - ns_):
        out.append({"kind": "removed", "name": k[1], "link": k[0]})
    for k in sorted(ns_ - os_):
        out.append({"kind": "added", "name": k[1], "link": k[0]})
    return out


IMPORTANT_PROPS = {
    "Execution.IsReentrant": "Reentrant",
    "Execution.Priority": "Priority",
    "Execution.PrefExecSyst": "Preferred execution system",
    "Execution.IsSubroutine": "Subroutine",
    "Execution.RunOnOpen": "Run when opened",
    "Execution.ShowFPOnCall": "Show front panel when called",
    "Execution.CloseAfterCall": "Close afterwards if originally closed",
    "Execution.DebugCapable": "Allow debugging",
    "Execution2.InlineIfPossible": "Inline",
    "Execution2.DefaultErrorHandling": "Automatic error handling",
    "Execution.PooledReentrancy": "Shared clone reentrancy",
    "Library.Protected": "Password protection",
    "FrontPanel.ShowScrollBar": "Scroll bars",
    "FrontPanel.ShowTitleBar": "Title bar",
    "FrontPanel.ShowMenuBar": "Menu bar",
    "FrontPanel.AutoCenter": "Auto center",
}

VOLATILE_PROPS = {"Execution.State", "Execution.BadSignal", "Execution.BadNode", "Execution.BadSubVILink",
                  "Execution.BadSubVI", "Execution.BadCompile", "Execution.CtlChanged",
                  "Execution2.UndoRedoChangedNoTypes", "Execution2.TaggedAndNotEdited",
                  "Execution2.RecalcFPDSOs", "Execution2.UndoRedoInProgress", "Execution.HasSetBP",
                  "Execution2.TransactionFailing", "Execution.BadDDO", "Execution.PropTypesIssues",
                  "Execution2.InlinableDiagram", "Execution.BrokenPolyVI", "Execution.HasInsaneItems",
                  "Execution.LibIssuesNoRun"}


def diff_props(old, new):
    out = []
    for k in sorted(set(old) | set(new)):
        if k in VOLATILE_PROPS:
            continue
        if old.get(k) != new.get(k):
            out.append({"key": k, "label": IMPORTANT_PROPS.get(k, k), "old": old.get(k), "new": new.get(k),
                        "important": k in IMPORTANT_PROPS})
    return out


def diff_models(old, new):
    res = Differ(old, new).run()
    res["verdict"] = file_verdict(old, new, res)
    return res


# ---------------------------------------------------------------------------
# File-level verdict: which part of the VI changed?
#   functional : logic/interface changed (node, wire, constant, control, connector pane, dependency, important property)
#   layout     : visual/layout change or a change whose details cannot be extracted (placement, size, icon, default value…)
#   saved      : only re-saved / recompiled (revision, compiled code, internal numbers)
# ---------------------------------------------------------------------------
LEVELS = {"saved": 0, "layout": 1, "functional": 2}
GEOM_TOLERANCE = 2  # pixels: font metric differences between computers


def compare_heap(a, b):
    if a is None and b is None:
        return "same"
    if a is None or b is None:
        return "content"
    if a["strict"] == b["strict"]:
        return "same"
    if a["loose"] == b["loose"]:
        ga, gb = a.get("geom") or [], b.get("geom") or []
        if a.get("route") == b.get("route") and len(ga) == len(gb) and \
                all(abs(x - y) <= GEOM_TOLERANCE for x, y in zip(ga, gb)):
            return "same"
        return "layout"
    return "content"


def _diff_conpane(a, b):
    if not a and not b:
        return []
    a = a or {"pattern": None, "terms": {}}
    b = b or {"pattern": None, "terms": {}}
    out = []
    if a.get("pattern") != b.get("pattern"):
        out.append("pattern changed")
    ta, tb = a.get("terms", {}), b.get("terms", {})
    for k in sorted(set(ta) | set(tb), key=lambda x: int(x) if str(x).isdigit() else 0):
        if ta.get(k) != tb.get(k):
            if k not in ta:
                out.append("terminal %s ← “%s” connected" % (k, tb[k]))
            elif k not in tb:
                out.append("terminal %s “%s” disconnected" % (k, ta[k]))
            else:
                out.append("terminal %s: “%s” → “%s”" % (k, ta[k], tb[k]))
    return out


def file_verdict(old, new, res):
    """Builds a per-section verdict from two VI models and the diff result."""
    parts = []

    def add(key, label, level, detail=""):
        parts.append({"key": key, "label": label, "level": level, "detail": detail})

    for side, m in (("old", old), ("new", new)):
        if m.get("error"):
            add("error", "Could not be parsed (%s version)" % side, "functional", m["error"])
    if old.get("empty") and not new.get("empty"):
        add("file", "New file", "functional")
    if new.get("empty") and not old.get("empty"):
        add("file", "File deleted", "functional")

    c = res["counts"]
    fo, fn = old.get("fingerprint") or {}, new.get("fingerprint") or {}
    both = not old.get("empty") and not new.get("empty") and not old.get("error") and not new.get("error")

    if both:
        # Block diagram
        nodes = c.get("added", 0) + c.get("removed", 0) + c.get("modified", 0)
        wires = c.get("wire_added", 0) + c.get("wire_removed", 0) + c.get("wire_modified", 0)
        notes = c.get("note_added", 0) + c.get("note_removed", 0)
        bits = []
        if c.get("added"):
            bits.append("+%d" % c["added"])
        if c.get("removed"):
            bits.append("−%d" % c["removed"])
        if c.get("modified"):
            bits.append("~%d" % c["modified"])
        if wires:
            bits.append("%d wire(s)" % wires)
        heap_bd = compare_heap(fo.get("bd"), fn.get("bd"))
        if nodes or wires:
            names_ = [ch["title"].split(" modified")[0] for ch in res["changes"] if ch["kind"] == "modified" and ch.get("cat") != "auto"][:3]
            add("bd", "Block diagram", "functional", " ".join(bits) + ((" · " + ", ".join(names_)) if names_ else ""))
        if notes:
            add("bd_notes", "Block diagram comments", "layout", "%d comment(s) added/removed" % notes)
        if c.get("moved"):
            moved_names = [ch["title"].replace(" moved", "").replace(" resized", "")
                           for ch in res["changes"] if ch["kind"] == "moved"]
            add("bd_layout", "Block diagram layout", "layout", "%d item(s) moved/resized: %s%s" % (
                c["moved"], ", ".join(moved_names[:4]), " …" if len(moved_names) > 4 else ""))
        elif heap_bd == "layout":
            add("bd_layout", "Block diagram layout", "layout", "placement / wire routing")
        if c.get("auto"):
            an = []
            for ch in res["changes"]:
                if ch.get("cat") == "auto":
                    nm = ch["title"].replace(" modified", "")
                    if nm not in an:
                        an.append(nm)
            add("bd_auto", AUTO_LABEL, "layout", "%d item(s): %s%s" % (c["auto"], ", ".join(an[:4]), " …" if len(an) > 4 else ""))
        if not (nodes or wires or notes or c.get("moved") or c.get("auto")) and heap_bd == "content":
            add("bd_other", "Block diagram (details not available)", "layout",
                "e.g. property node item, label visibility, structure setting")

        # Front panel
        heap_fp = compare_heap(fo.get("fp"), fn.get("fp"))
        fp_td = [x for x in res["fp"] if x["kind"] == "modified" and x["detail"] == "typedef contents updated"]
        fp_look = [x for x in res["fp"] if x["kind"] == "modified" and x["detail"] == "appearance/property changed"]
        fp_func = [x for x in res["fp"] if x not in fp_td and x not in fp_look]
        if fp_td:
            add("fp_typedef", "Typedef update", "layout",
                ", ".join("“%s”" % x["name"] for x in fp_td[:5]) + " — source .ctl changed, VI re-saved")
        if fp_func:
            add("fp", "Front panel controls", "functional",
                ", ".join("“%s”: %s" % (x["name"], x["detail"]) for x in fp_func[:4]) + (" …" if len(fp_func) > 4 else ""))
        if fp_look:
            add("fp_look", "Front panel appearance", "layout", ", ".join("“%s”" % x["name"] for x in fp_look[:5]))
        # moved/resized controls
        ob = {x["name"]: x.get("bounds") for x in old.get("fp", [])}
        moved_fp = []
        for x in new.get("fp", []):
            a, b = ob.get(x["name"]), x.get("bounds")
            if a and b and any(abs(p - q) > GEOM_TOLERANCE for p, q in zip(a, b)):
                moved_fp.append(x["name"])
        if moved_fp:
            add("fp_layout", "Front panel layout", "layout", "moved/resized: " +
                ", ".join("“%s”" % n for n in moved_fp[:6]) + (" …" if len(moved_fp) > 6 else ""))
        elif heap_fp == "layout" and not res["fp"]:
            add("fp_layout", "Front panel layout", "layout", "placement / decorations")
        if heap_fp == "content" and not res["fp"]:
            add("fp_other", "Front panel (control properties/appearance)", "layout")

        cp = _diff_conpane(old.get("conpane"), new.get("conpane"))
        if cp:
            add("conpane", "Connector pane", "functional", "; ".join(cp))
        if res["deps"]:
            add("deps", "Dependencies", "functional",
                ", ".join(("+" if d["kind"] == "added" else "−") + d["name"] for d in res["deps"][:4]) +
                (" …" if len(res["deps"]) > 4 else ""))
        imp = [p for p in res["props"] if p["important"]]
        other = [p for p in res["props"] if not p["important"]]
        if imp:
            add("props", "VI properties", "functional", ", ".join("%s: %s→%s" % (p["label"], p["old"], p["new"]) for p in imp))
        if other:
            add("props_other", "Other VI settings", "layout", ", ".join(p["key"].split(".")[-1] for p in other[:5]))
        if fo.get("data") and fn.get("data") and fo["data"] != fn["data"]:
            add("data", "Default values / data space", "layout")
        if fo.get("icon") != fn.get("icon") and fo.get("icon") and fn.get("icon"):
            add("icon", "Icon", "layout")
        mo, mn = old.get("meta", {}), new.get("meta", {})
        if (mo.get("description") or "") != (mn.get("description") or ""):
            add("desc", "VI description", "layout")
        if mo.get("lv_version") != mn.get("lv_version"):
            add("version", "LabVIEW version", "saved", "%s → %s" % (mo.get("lv_version"), mn.get("lv_version")))

    for p in parts:
        p["cat"] = PART_CATEGORY.get(p["key"], "other")
    level = max([LEVELS[p["level"]] for p in parts] + [0])
    name = {0: "saved", 1: "layout", 2: "functional"}[level]
    cats = sorted({p["cat"] for p in parts if p["key"] != "version"})
    return {"level": name, "parts": parts, "cats": cats or (["saved"] if name == "saved" else [])}


# Categories for user filters
CATEGORIES = [
    ("logic", "Block diagram logic"),
    ("bd_layout", "Block diagram layout"),
    ("fp", "Front panel"),
    ("auto", "Timed Loop / XNode internal settings"),
    ("other", "Other (icon, description, settings)"),
    ("saved", "Re-saved only"),
]
PART_CATEGORY = {
    "bd": "logic", "conpane": "logic", "deps": "logic", "props": "logic", "file": "logic", "error": "logic",
    "bd_layout": "bd_layout", "bd_other": "bd_layout", "bd_notes": "bd_layout",
    "fp": "fp", "fp_look": "fp", "fp_other": "fp", "fp_layout": "fp", "fp_typedef": "fp", "data": "fp",
    "bd_auto": "auto",
    "icon": "other", "desc": "other", "props_other": "other", "version": "other",
}
# Block diagram change kind -> category
CHANGE_CATEGORY = {"added": "logic", "removed": "logic", "modified": "logic", "wire_added": "logic",
                   "wire_removed": "logic", "wire_modified": "logic", "note_added": "bd_layout",
                   "note_removed": "bd_layout", "moved": "bd_layout"}

