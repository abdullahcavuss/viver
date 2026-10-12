"""
Parses a LabVIEW VI file without LabVIEW.

Uses pylabview (third_party/pylabview) to extract the VI into XML parts, then
reads the block diagram heap (BDHb) and the front panel heap (FPHb) to build a
drawable model:

    {
      "meta":   {...version, VI properties...},
      "icon":   "data:image/png;base64,...",
      "fp":     [ {uid, name, ctype, bounds} ],          # front panel controls
      "deps":   [ {kind, name, path} ],                  # subVIs / dependencies
      "bd":     <diagram>                                # root block diagram
    }

    <diagram> = {"uid", "nodes": [<node>], "wires": [<wire>], "notes": [<note>]}
    <node>    = {"uid", "cls", "kind", "name", "sig", "x","y","w","h",
                 "terms": [{"uid","idx","x","y"}], "frames": [{"name", "diagram"}], "active"}
    <wire>    = {"uid", "cat", "arr", "ends": [{"term", "node", "key", "x", "y"}]}
"""
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from . import names

HERE = os.path.dirname(os.path.abspath(__file__))
THIRD_PARTY = os.path.join(os.path.dirname(HERE), "third_party")

VI_EXTENSIONS = (".vi", ".vit", ".ctl", ".ctt", ".vim")

PARSER_VERSION = 18  # bump when the model format changes, to invalidate the cache


class VIParseError(Exception):
    pass


# --------------------------------------------------------------------------
# extraction with pylabview
# --------------------------------------------------------------------------

def extract_with_pylabview(vi_path, out_dir, timeout=180):
    """Extracts the VI into XML parts inside out_dir and returns the path of the main xml."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, "vi")
    env = dict(os.environ)
    env["PYTHONPATH"] = THIRD_PARTY + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-W", "ignore", "-m", "pylabview.readRSRC",
           "-x", "-i", vi_path, "-m", base + ".xml"]
    try:
        proc = subprocess.run(cmd, cwd=out_dir, env=env, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise VIParseError("pylabview timed out")
    main_xml = base + ".xml"
    if not os.path.exists(main_xml):
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        raise VIParseError("pylabview could not open the VI: " + " | ".join(msg))
    warnings = [l for l in (proc.stderr or "").splitlines() if "Warning" in l or "Error" in l]
    return main_xml, warnings


def _parse_xml(path, comments=False):
    if comments:
        parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
        return ET.parse(path, parser=parser).getroot()
    return ET.parse(path).getroot()


def _rect(text):
    """'(top, left, bottom, right)' -> (t, l, b, r)"""
    if not text:
        return None
    try:
        parts = [int(p) for p in text.strip().strip("()").split(",")]
        if len(parts) == 4:
            return tuple(parts)
    except ValueError:
        pass
    return None


def _clean_text(t):
    if t is None:
        return None
    t = t.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        t = t[1:-1]
    t = t.replace("\x00", "").strip()
    return t or None


def _first_text(el):
    """Text inside a label's textRec."""
    if el is None:
        return None
    for tr in el.iter("textRec"):
        tx = tr.find("text")
        if tx is not None and tx.text:
            return _clean_text(tx.text)
    return None


# --------------------------------------------------------------------------
# Main XML: meta, types, dependencies
# --------------------------------------------------------------------------

class TypeInfo:
    """Heap TypeID -> data type (for the wire color)."""

    def __init__(self, main_xml_path):
        self.heap = {}       # heap id -> consolidated id
        self.heap_name = {}  # heap id -> type name (from the comment)
        self.flat = {}       # flat id -> (type, elem flat id)
        self.inner_label = {}  # typedef flat id -> label of the inner control
        self.top = {}        # consolidated index -> flat id
        try:
            raw = open(main_xml_path, encoding="utf-8", errors="replace").read()
        except OSError:
            return
        for m in re.finditer(r"Heap TypeID\s+(\d+)\s*=\s*Consolidated TypeID\s+(\d+):\s*([\w ]+?)\s*-->", raw):
            self.heap[int(m.group(1))] = int(m.group(2))
            self.heap_name[int(m.group(1))] = m.group(3).strip()
        vctp = re.search(r"<VCTP>(.*?)</VCTP>", raw, re.S)
        if vctp:
            body = vctp.group(1)
            heads = list(re.finditer(r"<!-- FlatTypeID (\d+):[^>]*-->\s*<TypeDesc ([^>]*?)(/?)>", body))
            for i, m in enumerate(heads):
                fid = int(m.group(1))
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(2)))
                end = heads[i + 1].start() if i + 1 < len(heads) else body.find("<TopLevel")
                span = body[m.end():end if end > 0 else m.end() + 4000]
                elem = None
                if attrs.get("Type") == "Array" and m.group(3) != "/":
                    sub = re.search(r'<TypeDesc TypeID="(\d+)"', span[:600])
                    if sub:
                        elem = int(sub.group(1))
                enum = re.findall(r"<EnumLabel>(.*?)</EnumLabel>", span) if m.group(3) != "/" else []
                inner_type = None
                if attrs.get("Type") == "TypeDef":
                    it = re.search(r'<TypeDesc Type="(\w+)"', span)
                    inner_type = it.group(1) if it else None
                    il = re.search(r'<TypeDesc Type="\w+"[^>]*?Label="([^"]*)"', span)
                    if il:
                        self.inner_label[fid] = il.group(1)
                self.flat[fid] = (attrs.get("Type", ""), elem, attrs.get("Label"), enum, inner_type)
            for m in re.finditer(r'<TypeDesc Index="(\d+)" FlatTypeID="(\d+)"', body):
                self.top[int(m.group(1))] = int(m.group(2))

    def wire_style(self, heap_type_text):
        """'TypeID(65)' -> (category, is_array)"""
        if not heap_type_text:
            return "other", False
        m = re.search(r"(\d+)", heap_type_text)
        if not m:
            return "other", False
        hid = int(m.group(1))
        tname = self.heap_name.get(hid)
        cid = self.heap.get(hid)
        fid = self.top.get(cid) if cid is not None else None
        if fid is not None and fid in self.flat:
            tname, elem, _, _, inner = self.flat[fid]
            if tname == "TypeDef" and inner:
                tname = inner
            if tname == "Array" and elem is not None and elem in self.flat:
                return names.wire_category(self.flat[elem][0]), True
        cat = names.wire_category(tname)
        return cat, cat == "array"

    def resolve(self, heap_type_text):
        """Heap type reference -> (type_name, enum_labels, array_element_type)"""
        m = re.search(r"(\d+)", heap_type_text or "")
        if not m:
            return None, [], None
        hid = int(m.group(1))
        cid = self.heap.get(hid)
        fid = self.top.get(cid) if cid is not None else None
        if fid is None or fid not in self.flat:
            return self.heap_name.get(hid), [], None
        tname, elem, _, enum, inner = self.flat[fid]
        if tname == "TypeDef" and inner:
            tname = inner
        elem_t = self.flat[elem][0] if elem is not None and elem in self.flat else None
        return tname, enum, elem_t

    def label_of(self, heap_type_text):
        """Label of a heap type reference in the type table (control name)."""
        m = re.search(r"(\d+)", heap_type_text or "")
        if not m:
            return None
        cid = self.heap.get(int(m.group(1)))
        fid = self.top.get(cid) if cid is not None else None
        if fid is not None and fid in self.flat:
            return self.flat[fid][2] or self.inner_label.get(fid)
        return None

    def selector_info(self, heap_type_text):
        """Case selector type: ('bool'|'enum'|'other', enum_labels)"""
        m = re.search(r"(\d+)", heap_type_text or "")
        if not m:
            return "other", []
        hid = int(m.group(1))
        cid = self.heap.get(hid)
        fid = self.top.get(cid) if cid is not None else None
        if fid is not None and fid in self.flat:
            tname, _, _, enum, inner = self.flat[fid]
            if tname == "Boolean" or inner == "Boolean":
                return "bool", []
            if enum:
                return "enum", enum
        if self.heap_name.get(hid) == "Boolean":
            return "bool", []
        return "other", []


def _parse_meta(root):
    meta = {}
    lvsr = root.find("LVSR/Section")
    if lvsr is None:
        lvsr = root.find("vers/Section")
    if lvsr is not None:
        ver = lvsr.find("Version")
        if ver is not None:
            meta["lv_version"] = "{}.{}.{}".format(ver.get("Major"), ver.get("Minor"), ver.get("Bugfix"))
        meta["file_name"] = lvsr.get("Name")
        props = {}
        for tag in ("Execution", "Execution2", "FrontPanel", "Library"):
            el = lvsr.find(tag)
            if el is not None:
                for k, v in el.attrib.items():
                    if k.startswith("ViBhBit") or k.startswith("InStBit") or k == "PasswordHash":
                        continue
                    props[tag + "." + k] = v
        lib = lvsr.find("Library")
        if lib is not None:
            meta["locked"] = lib.get("Protected") not in (None, "0")
        meta["props"] = props
    vers = root.find("vers/Section/Version")
    if vers is not None and vers.get("Info"):
        meta["lv_version_str"] = vers.get("Info")
    meta["file_type"] = root.get("Type")
    # Documentation (VI description)
    strg = root.find("STRG/Section")
    if strg is not None:
        s = strg.find("String")
        if s is not None and s.text:
            meta["description"] = s.text.strip()
    return meta


def _parse_deps(root):
    deps = []
    seen = set()
    for block in ("BDHP", "LIvi", "LIbd"):
        for sect in root.findall(block + "/Section"):
            for holder in sect:
                for link in holder:
                    q = link.find("LinkSaveQualName")
                    if q is None:
                        continue
                    nm = ":".join(s.text for s in q.findall("String") if s.text)
                    path_el = link.find("LinkSavePathRef")
                    path = ""
                    if path_el is not None:
                        path = "/".join(s.text for s in path_el.findall("String") if s.text)
                    key = (link.tag, nm)
                    if not nm or key in seen:
                        continue
                    seen.add(key)
                    deps.append({"kind": link.tag, "name": nm, "path": path, "block": block})
    return deps


def _iuse_names(root):
    """Order of the IUse->VI links in BDHP (for naming when there is no label)."""
    out = []
    for link in root.iter("IUVI"):
        q = link.find("LinkSaveQualName")
        if q is not None:
            out.append(":".join(s.text for s in q.findall("String") if s.text))
    return out


def _icon(out_dir):
    for pat in ("*_icl8.png", "*_icl4.png", "*_ICON.png"):
        files = glob.glob(os.path.join(out_dir, pat))
        if files:
            with open(files[0], "rb") as f:
                return "data:image/png;base64," + base64.b64encode(f.read()).decode()
    return None


# --------------------------------------------------------------------------
# Front panel
# --------------------------------------------------------------------------

def _parse_fp(fp_path):
    """FP heap: dco uid -> {name, ctype, bounds}"""
    out = {}
    if not fp_path or not os.path.exists(fp_path):
        return out
    root = _parse_xml(fp_path)
    for el in root.iter():
        if el.get("class") != "fPDCO" or len(el) == 0:
            continue
        ddo = el.find("ddo")
        name = None
        ctype = None
        bounds = None
        if ddo is not None:
            ctype = ddo.get("class")
            bounds = _rect(ddo.findtext("bounds"))
            pl = ddo.find("partsList")
            if pl is not None:
                for p in pl:
                    if p.get("class") == "label":
                        name = _first_text(p)
                        if name:
                            break
            if not name:
                name = _first_text(ddo)
        try:
            flags = int(el.findtext("objFlags") or 0)
        except ValueError:
            flags = 0
        has_td = ctype == "typeDef" or (ddo is not None and any(x.get("class") == "typeDef" for x in ddo.iter()))
        out[el.get("uid")] = {
            "uid": el.get("uid"),
            "chash": _subtree_hash(el),
            "typedef": has_td,
            "name": name or "(unnamed)",
            "ctype": ctype,
            "ctype_name": names.DDO_CLASS_NAMES.get(ctype, ctype),
            "bounds": bounds,
            "flags": flags,
        }
    return out


# --------------------------------------------------------------------------
# Block diagram
# --------------------------------------------------------------------------

_INT_SIZES = {"Int8": 1, "UInt8": 1, "Int16": 2, "UInt16": 2, "Int32": 4, "UInt32": 4, "Int64": 8, "UInt64": 8}


def _int_size(tname):
    for k, v in sorted(_INT_SIZES.items(), key=lambda kv: -len(kv[0])):
        if tname and tname.endswith(k.replace("Int", "Int")) and k in tname:
            return v, not k.startswith("U")
    return None, False


def _lv_text(b):
    for enc in ("utf-8", "cp1254", "cp1252"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("latin-1")


def _short(t, n=60):
    t = t.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return t if len(t) <= n else t[:n - 1] + "…"


def _decode_scalar(b, tname, enum):
    """A single value from a byte array. Returns: (display text, bytes consumed)"""
    import struct
    t = tname or ""
    if t == "Boolean":
        return ("True" if any(b[:1]) else "False"), 1
    if t.startswith("NumFloat64") or t.startswith("UnitFloat64"):
        return ("%g" % struct.unpack(">d", b[:8])[0] if len(b) >= 8 else "0"), 8
    if t.startswith("NumFloat32") or t.startswith("UnitFloat32"):
        return ("%g" % struct.unpack(">f", b[:4])[0] if len(b) >= 4 else "0"), 4
    if t in ("String", "CString", "PasString"):
        if len(b) < 4:
            return '""', len(b)
        n = int.from_bytes(b[:4], "big")
        return '"%s"' % _short(_lv_text(b[4:4 + n])), 4 + n
    size, signed = _int_size(t)
    if size:
        chunk = b[:size]
        v = int.from_bytes(chunk, "big", signed=signed) if len(chunk) == size else 0
        if enum and 0 <= v < len(enum):
            return '"%s"' % enum[v], size
        return str(v), size
    return None, 0


def decode_const(raw, tname, enum, elem_t, ctype):
    """Converts the ConstValue (hex) of a block diagram constant into readable text."""
    try:
        b = bytes.fromhex(raw)
    except ValueError:
        return raw[:40]
    t = tname or ""
    if not any(b) and t not in ("Path",):
        # LabVIEW stores zero values in a shortened form
        if t == "Boolean":
            return "False"
        if t in ("String", "CString", "PasString"):
            return '""'
        if t == "Array":
            return "[ ]"
        if enum:
            return '"%s"' % enum[0]
        if t == "Cluster":
            return "{default}"
        return "0"
    if t == "Path" and b[:4] == b"PTH0":
        try:
            pos = 12
            parts = []
            cnt = int.from_bytes(b[10:12], "big")
            for _ in range(cnt):
                n = b[pos]
                parts.append(_lv_text(b[pos + 1:pos + 1 + n]))
                pos += 1 + n
            return _short("\\".join(parts) or "<empty path>")
        except Exception:
            return "<path>"
    if t == "Array":
        n = int.from_bytes(b[:4], "big") if len(b) >= 4 else 0
        items, pos = [], 4
        if elem_t:
            for _ in range(min(n, 6)):
                v, used = _decode_scalar(b[pos:], elem_t, [])
                if v is None or not used:
                    break
                items.append(v)
                pos += used
        body = ", ".join(items) + (", …" if n > len(items) and items else "")
        return "[%s]" % body if items else "[%d items]" % n
    v, _ = _decode_scalar(b, t, enum)
    if v is not None:
        return v
    return "0x" + raw[:16] + ("…" if len(raw) > 16 else "")


class BDWalker:
    def __init__(self, bd_root, types, fp_map, iuse_names, prim_names):
        self.root = bd_root
        self.types = types
        self.fp = fp_map
        self.iuse_names = list(iuse_names)
        self.iuse_counter = 0
        self.prim_names = prim_names or {}
        self.by_uid = {}
        for el in bd_root.iter():
            u = el.get("uid")
            if u is not None and len(el) > 0 and u not in self.by_uid:
                self.by_uid[u] = el
        self.terms = {}       # term uid -> endpoint dict
        self.dco_owner = {}   # dco uid -> (structure uid, ox, oy, idx)
        self.struct_nodes = {}  # structure uid -> node
        self.own_name = ""
        self.stats = {"nodes": 0, "wires": 0, "unresolved_ends": 0}

    def resolve(self, el):
        if el is None:
            return None
        if len(el) == 0 and el.get("uid") in self.by_uid:
            return self.by_uid[el.get("uid")]
        return el

    # ---- helpers
    def _dco_of_term(self, term_el):
        d = term_el.find("dco")
        if d is None:
            # some terms are stored directly under another class tag
            for c in term_el:
                if c.tag.endswith("DCO") or c.tag == "dco":
                    d = c
                    break
        return self.resolve(d) if d is not None else None

    def _term_point(self, dco, ox, oy, fallback):
        tb = _rect(dco.findtext("termBounds")) if dco is not None else None
        if tb:
            t, l, b, r = tb
            return ox + (l + r) / 2.0, oy + (t + b) / 2.0
        return fallback

    def _register_terms(self, node, node_el, ox, oy):
        """Records the terminals of a node (ox, oy = absolute top-left of the node)."""
        tl = node_el.find("termList")
        if tl is None:
            return
        cx = node["x"] + node["w"] / 2.0
        cy = node["y"] + node["h"] / 2.0
        for idx, term in enumerate(tl):
            term = self.resolve(term)
            if term is None:
                continue
            dco = self._dco_of_term(term)
            x, y = self._term_point(dco, ox, oy, (cx, cy))
            info = {"term": term.get("uid"), "node": node["uid"], "key": "%s:%d" % (node["uid"], idx),
                    "x": x, "y": y}
            self.terms[term.get("uid")] = info
            tinfo = {"uid": term.get("uid"), "idx": idx, "x": x, "y": y}
            tb = _rect(dco.findtext("termBounds")) if dco is not None else None
            if tb:
                tinfo["w"] = tb[3] - tb[1]
                tinfo["h"] = tb[2] - tb[0]
            if dco is not None and dco.get("class"):
                tinfo["cls"] = dco.get("class")
            node["terms"].append(tinfo)
            if dco is not None and dco.get("uid"):
                self.dco_owner[dco.get("uid")] = (node["uid"], ox, oy, idx)

    def _variable_info(self, el, ctx_label):
        """Global/local variable node: (control name, owner VI, is local, is read)"""
        tl = el.find("termList")
        term = self.resolve(tl[0]) if tl is not None and len(tl) else None
        dco = self._dco_of_term(term) if term is not None else None
        name = None
        owner = (ctx_label or "").split(":")[-1]
        local = bool(owner) and bool(self.own_name) and owner.lower() == self.own_name.lower()
        idx = dco.findtext("paramIdx") if dco is not None else None
        if dco is not None:
            name = self.types.label_of(dco.findtext("typeDesc"))
        if not name and local:
            # local variable: from its own front panel, by data space order
            fps = sorted(self.fp.values(), key=lambda c: int(c["uid"]))
            try:
                name = fps[int(idx or 0)]["name"]
            except (ValueError, KeyError, IndexError):
                name = None
        if not name:
            name = "item #%s" % (idx or "0")
        # terminal flag 0x8000 = input terminal -> the variable is written
        try:
            flags = int(term.findtext("objFlags") or 0) if term is not None else 0
        except ValueError:
            flags = 0
        read = not (flags & 0x8000)
        return name, owner, local, read

    def _node_name(self, cls, el, ctx_label):
        if cls == "gRef":
            name, owner, local, read = self._variable_info(el, ctx_label)
            self._last_var = {"var": name, "owner": owner, "local": local, "read": read}
            return name, "var:%s:%s:%s" % ("L" if local else "G", owner.lower(), name.lower())
        if cls in names.SUBVI_CLASSES:
            nm = ctx_label
            if self.iuse_counter < len(self.iuse_names):
                order_nm = self.iuse_names[self.iuse_counter]
            else:
                order_nm = None
            self.iuse_counter += 1
            nm = nm or order_nm or "SubVI"
            return nm, "subvi:" + nm.lower()
        if cls == "prim":
            rid = el.findtext("primResID") or el.findtext("primIndex") or "?"
            nm = self.prim_names.get(str(rid))
            return (nm or "Prim %s" % rid), "prim:" + str(rid)
        nice = names.NODE_CLASS_NAMES.get(cls, cls)
        if ctx_label and cls in ("propNode", "invokeNode", "callByRefNode", "extFunc"):
            return "%s: %s" % (nice, ctx_label), cls + ":" + ctx_label.lower()
        return nice, cls

    def _frame_names(self, el, count):
        cls = el.get("class")
        fnames = [None] * count
        if cls == "select":
            sel_kind, enum_labels = "other", []
            cs = self.resolve(el.find("caseSelDCO"))
            if cs is not None:
                sel_kind, enum_labels = self.types.selector_info(cs.findtext("typeDesc"))

            def val(v):
                if v is None:
                    return "?"
                try:
                    iv = int(v)
                except ValueError:
                    return v
                if sel_kind == "bool":
                    return "True" if iv else "False"
                if sel_kind == "enum" and 0 <= iv < len(enum_labels):
                    return '"%s"' % enum_labels[iv]
                return str(iv)
            ranges = el.find("SelectRangeArray32")
            if ranges is None:
                ranges = el.find("SelectRangeArray")
            if ranges is not None:
                for r in ranges:
                    try:
                        di = int(r.findtext("diagramIdx") or -1)
                        s, e = r.findtext("start"), r.findtext("end")
                    except ValueError:
                        continue
                    if 0 <= di < count:
                        lab = val(s) if s == e else "%s..%s" % (val(s), val(e))
                        fnames[di] = lab if fnames[di] is None else fnames[di] + ", " + lab
            strs = el.find("SelectStringArray")
            if strs is not None:
                for r in strs:
                    txt = _clean_text(r.findtext("string") or r.findtext("text") or (r.text or ""))
                    try:
                        di = int(r.findtext("diagramIdx") or -1)
                    except ValueError:
                        di = -1
                    if txt and 0 <= di < count:
                        fnames[di] = '"%s"' % txt if fnames[di] is None else fnames[di] + ', "%s"' % txt
            try:
                dflt = int(el.findtext("SelectDefaultCase") or "-1", 16)
            except ValueError:
                dflt = -1
            if 0 <= dflt < count and sel_kind != "bool":
                fnames[dflt] = (fnames[dflt] + ", Default") if fnames[dflt] else "Default"
        elif cls == "eventStruct":
            fnames = ["Event %d" % i for i in range(count)]
        elif cls in ("sequence", "flatSequence", "timeFlatSequence"):
            fnames = ["Frame %d" % i for i in range(count)]
        elif cls == "commentNode":
            fnames = ["Disabled" if i == 0 else "Enabled" for i in range(count)]
        return [f if f is not None else "Frame %d" % i for i, f in enumerate(fnames)]

    # ---- main walk
    def walk_diagram(self, diag_el, ox, oy, owner_uid=None):
        diagram = {"uid": diag_el.get("uid"), "nodes": [], "wires": [], "notes": []}
        objs = []
        seen = set()
        for lst in ("zPlaneList", "nodeList"):
            l = diag_el.find(lst)
            if l is None:
                continue
            for e in l:
                r = self.resolve(e)
                if r is None or r.get("uid") in seen:
                    continue
                seen.add(r.get("uid"))
                objs.append(r)

        for el in objs:
            cls = el.get("class")
            if cls is None or cls in names.DECOR_CLASSES:
                continue
            if cls == "sRN":
                self._handle_srn(el, diagram, ox, oy, owner_uid)
                continue
            if cls == "label" or cls == "multiLabel" or cls == "bigMultiLabel":
                txt = _first_text(el)
                b = _rect(el.findtext("bounds"))
                if txt and b:
                    t, l, bb, r = b
                    diagram["notes"].append({"uid": el.get("uid"), "text": txt,
                                             "x": ox + l, "y": oy + t, "w": r - l, "h": bb - t})
                continue
            if cls == "fPTerm":
                self._handle_fpterm(el, diagram, ox, oy)
                continue
            b = _rect(el.findtext("bounds"))
            if b is None or (el.find("termList") is None and el.find("diagramList") is None):
                continue
            t, l, bb, r = b
            label_el = el.find("label")
            ctx_label = _first_text(label_el)
            nm, sig = self._node_name(cls, el, ctx_label)
            kind = "structure" if el.find("diagramList") is not None else (
                "subvi" if cls in names.SUBVI_CLASSES else ("prim" if cls == "prim" else "node"))
            if cls == "gRef":
                kind = "variable"
            node = {"uid": el.get("uid"), "cls": cls, "kind": kind, "name": nm, "sig": sig,
                    "x": ox + l, "y": oy + t, "w": r - l, "h": bb - t, "terms": []}
            if cls == "gRef" and getattr(self, "_last_var", None):
                node.update(self._last_var)
            if cls == "prim":
                node["prim"] = el.findtext("primResID") or el.findtext("primIndex")
            node["chash"] = _subtree_hash(el, skip=("diagramList", "label", "termList", "tunnelList"))
            if kind == "structure":
                self.struct_nodes[node["uid"]] = node
            self._register_terms(node, el, ox + l, oy + t)
            dl = el.find("diagramList")
            if dl is not None:
                frames_el = [self.resolve(d) for d in dl]
                fnames = self._frame_names(el, len(frames_el))
                node["frames"] = []
                for i, d in enumerate(frames_el):
                    if d is None:
                        continue
                    sub = self.walk_diagram(d, ox + l, oy + t, owner_uid=node["uid"])
                    node["frames"].append({"name": fnames[i], "diagram": sub})
                try:
                    node["active"] = int(el.findtext("dIdx") or 0)
                except ValueError:
                    node["active"] = 0
                if node["active"] >= len(node["frames"]):
                    node["active"] = 0
                cr = _rect(el.findtext("contRect"))
                if cr:
                    node["inner"] = {"x": ox + cr[1], "y": oy + cr[0], "w": cr[3] - cr[1], "h": cr[2] - cr[0]}
            diagram["nodes"].append(node)
            self.stats["nodes"] += 1

        # wires
        sl = diag_el.find("signalList")
        if sl is not None:
            for sig in sl:
                sig = self.resolve(sig)
                if sig is None or sig.get("class") not in ("signal", "hSignal", "mergeSignal", "splitSignal", None):
                    continue
                tl = sig.find("termList")
                if tl is None:
                    continue
                ends = []
                types = []
                for tr in tl:
                    info = self.terms.get(tr.get("uid"))
                    if info is None:
                        self.stats["unresolved_ends"] += 1
                        continue
                    ends.append(dict(info))
                    term_el = self.by_uid.get(tr.get("uid"))
                    if term_el is not None:
                        dco = self._dco_of_term(term_el)
                        if dco is not None and dco.findtext("typeDesc"):
                            types.append(dco.findtext("typeDesc"))
                if len(ends) < 2:
                    continue
                cat, arr = ("other", False)
                for ty in types:
                    c, a = self.types.wire_style(ty)
                    if c not in ("other", "void"):
                        cat, arr = c, a
                        break
                    cat, arr = c, a
                wire = {"uid": sig.get("uid"), "cat": cat, "arr": arr, "ends": ends}
                diagram["wires"].append(wire)
                self.stats["wires"] += 1
        return diagram

    def _handle_srn(self, el, diagram, ox, oy, owner_uid):
        """Self-reference node: inner faces of tunnels and diagram constants."""
        tl = el.find("termList")
        if tl is None:
            return
        for term in tl:
            term = self.resolve(term)
            if term is None:
                continue
            if term.get("class") == "fPTerm":
                continue  # a front panel terminal is handled as its own node
            dco = self._dco_of_term(term)
            if dco is None:
                continue
            dcls = dco.get("class")
            if dcls == "bDConstDCO":
                ddo = dco.find("ddo")
                b = _rect(ddo.findtext("bounds")) if ddo is not None else None
                if not b:
                    continue
                t, l, bb, r = b
                label = None
                if ddo is not None:
                    pl = ddo.find("partsList")
                    if pl is not None:
                        for p in pl:
                            if p.get("class") in ("label", "multiLabel", "bigMultiLabel"):
                                label = _first_text(p)
                                if label:
                                    break
                ctype = ddo.get("class") if ddo is not None else None
                type_name = names.DDO_CLASS_NAMES.get(ctype, ctype or "")
                cat, arr = self.types.wire_style(dco.findtext("typeDesc"))
                raw = (dco.findtext("ConstValue") or "").strip()
                tname, enum, elem_t = self.types.resolve(dco.findtext("typeDesc"))
                value = decode_const(raw, tname, enum, elem_t, ctype) if raw else None
                name = label or type_name or "Constant"
                node = {"uid": term.get("uid"), "cls": "const", "kind": "const", "name": name,
                        "ctype": ctype, "sig": "const:%s" % ctype,
                        "cat": cat, "arr": arr, "raw": raw, "value": value, "vtype": tname,
                        "x": ox + l, "y": oy + t, "w": r - l, "h": bb - t, "terms": []}
                x = node["x"] + node["w"]
                y = node["y"] + node["h"] / 2.0
                node["terms"].append({"uid": term.get("uid"), "idx": 0, "x": x, "y": y})
                self.terms[term.get("uid")] = {"term": term.get("uid"), "node": node["uid"],
                                               "key": "%s:0" % node["uid"], "x": x, "y": y}
                diagram["nodes"].append(node)
                self.stats["nodes"] += 1
                continue
            owner = self.dco_owner.get(dco.get("uid"))
            if owner is None and dcls in ("lCnt", "lTst", "lpTunConditionDCO", "lTiming") and owner_uid in self.struct_nodes:
                sn = self.struct_nodes[owner_uid]
                tb = _rect(dco.findtext("termBounds"))
                if tb:
                    it = {"cls": dcls, "x": sn["x"] + tb[1], "y": sn["y"] + tb[0], "w": tb[3] - tb[1], "h": tb[2] - tb[0]}
                    if not any(i["cls"] == dcls for i in sn.setdefault("iterms", [])):
                        sn["iterms"].append(it)
                    x, y = sn["x"] + (tb[1] + tb[3]) / 2.0, sn["y"] + (tb[0] + tb[2]) / 2.0
                    self.terms[term.get("uid")] = {"term": term.get("uid"), "node": owner_uid,
                                                   "key": "%s:%s:i" % (owner_uid, dcls), "x": x, "y": y}
                    continue
            if owner is not None:
                s_uid, sox, soy, sidx = owner
                x, y = self._term_point(dco, sox, soy, (sox, soy))
                self.terms[term.get("uid")] = {"term": term.get("uid"), "node": s_uid,
                                               "key": "%s:%d:i" % (s_uid, sidx), "x": x, "y": y}
            else:
                x, y = self._term_point(dco, ox, oy, (ox, oy))
                self.terms[term.get("uid")] = {"term": term.get("uid"), "node": owner_uid or "?",
                                               "key": "%s:%s:i" % (owner_uid, dco.get("uid")), "x": x, "y": y}

    def _handle_fpterm(self, el, diagram, ox, oy):
        b = _rect(el.findtext("bounds"))
        if not b:
            return
        t, l, bb, r = b
        dco = self.resolve(el.find("dco"))
        fp = self.fp.get(dco.get("uid")) if dco is not None else None
        name = fp["name"] if fp else (_first_text(el.find("label")) or "Terminal")
        ctype = fp["ctype"] if fp else None
        node = {"uid": el.get("uid"), "cls": "fPTerm", "kind": "terminal", "name": name,
                "ctype": ctype, "ctype_name": names.DDO_CLASS_NAMES.get(ctype, ctype),
                "sig": "term:%s" % name.lower(), "fp_uid": dco.get("uid") if dco is not None else None,
                "x": ox + l, "y": oy + t, "w": r - l, "h": bb - t, "terms": []}
        try:
            flags = int(el.findtext("objFlags") or 0)
        except ValueError:
            flags = 0
        if fp is not None:
            node["io"] = "indicator" if fp["flags"] & 0x1 else "control"
        else:
            node["io"] = "indicator" if flags & 0xC000 else "control"
        cx = node["x"] + node["w"] / 2.0
        cy = node["y"] + node["h"] / 2.0
        node["terms"].append({"uid": el.get("uid"), "idx": 0, "x": cx, "y": cy})
        self.terms[el.get("uid")] = {"term": el.get("uid"), "node": node["uid"],
                                     "key": "%s:0" % node["uid"], "x": cx, "y": cy, "fpterm": True}
        diagram["nodes"].append(node)
        self.stats["nodes"] += 1


def _fix_terminal_sides(diagram):
    """On front panel terminals, move wire ends to the correct edge of the box and guess control/indicator."""
    nodes = {}

    def collect(d):
        for n in d["nodes"]:
            nodes[n["uid"]] = n
            for f in n.get("frames", []):
                collect(f["diagram"])

    collect(diagram)

    def fix(d):
        for w in d["wires"]:
            for e in w["ends"]:
                n = nodes.get(e["node"])
                if n is None or n["kind"] not in ("terminal",):
                    continue
                others = [o["x"] for o in w["ends"] if o is not e]
                if not others:
                    continue
                avg = sum(others) / len(others)
                if avg >= n["x"] + n["w"] / 2.0:
                    e["x"] = n["x"] + n["w"]
                    n.setdefault("_out", 0)
                    n["_out"] += 1
                else:
                    e["x"] = n["x"]
                    n.setdefault("_in", 0)
                    n["_in"] += 1
        for n in d["nodes"]:
            for f in n.get("frames", []):
                fix(f["diagram"])

    fix(diagram)
    for n in nodes.values():
        n.pop("_in", None)
        n.pop("_out", None)


def bd_bounds(diagram):
    xs, ys, xe, ye = [], [], [], []

    def walk(d):
        for n in d["nodes"]:
            xs.append(n["x"]); ys.append(n["y"]); xe.append(n["x"] + n["w"]); ye.append(n["y"] + n["h"])
            for f in n.get("frames", []):
                walk(f["diagram"])
        for n in d["notes"]:
            xs.append(n["x"]); ys.append(n["y"]); xe.append(n["x"] + n["w"]); ye.append(n["y"] + n["h"])

    walk(diagram)
    if not xs:
        return [0, 0, 400, 300]
    return [min(xs), min(ys), max(xe), max(ye)]


# ----------------------------------------------------------------------------
# Section fingerprints: for the question "what really changed?"
# ----------------------------------------------------------------------------
# Heap fields LabVIEW changes by itself when saving/compiling
_HEAP_NOISE = {"clumpNum", "firstNodeIdx", "oMId", "objFlags", "pBounds", "dBounds", "origin",
               "savedSize", "docBounds", "conNum", "paneFlags", "fontid", "i", "state", "lastSignalKind",
               "shortCount", "dIdx", "selString", "SelectSelLabFlags"}
# Geometry fields (position/size): count as a layout change, not content
_HEAP_GEOM = {"bounds", "termBounds", "contRect", "view"}
_HEAP_ROUTE = {"compressedWireTable"}
_TAG_RE = re.compile(r"^<([A-Za-z_][\w]*)(?:\s[^>]*)?>(.*?)</\1>$")


def _subtree_hash(el, skip=()):
    """Content digest of a heap item: excluding noise, uids, geometry, text formatting and 'skip' subtrees."""
    import hashlib
    h = hashlib.sha1()

    def walk(e):
        if e.tag in _HEAP_NOISE or e.tag in skip or e.tag == "fr" or e.get("class") == "fontRun":
            return
        if e.tag in _HEAP_GEOM or e.tag in _HEAP_ROUTE:
            return
        attrs = ",".join("%s=%s" % (k, v) for k, v in sorted(e.attrib.items()) if k != "uid")
        txt = re.sub(r"TypeID\(\d+\)", "TypeID()", (e.text or "").strip())
        h.update(("<%s %s>%s" % (e.tag, attrs, txt)).encode("utf-8", "replace"))
        for c in e:
            walk(c)
        h.update(b"</>")

    walk(el)
    return h.hexdigest()[:16]


def _heap_fingerprint(path):
    """From a heap XML tree:
    strict : full digest with noise fields and uid numbers removed
    loose  : additionally without geometry, wire routing and text formatting (content only)
    route  : wire routing + text formatting digest
    geom   : position/size numbers (for tolerance comparison)"""
    import hashlib
    if not path or not os.path.exists(path):
        return None
    try:
        root = _parse_xml(path)
    except ET.ParseError:
        return None
    strict, loose, route = hashlib.sha1(), hashlib.sha1(), hashlib.sha1()
    geom = []

    def tok(el):
        attrs = ",".join("%s=%s" % (k, v) for k, v in sorted(el.attrib.items()) if k != "uid")
        txt = (el.text or "").strip()
        txt = re.sub(r"TypeID\(\d+\)", "TypeID()", txt)
        return ("<%s %s>%s" % (el.tag, attrs, txt)).encode("utf-8", "replace")

    def walk(el, cosmetic):
        tag = el.tag
        if tag in _HEAP_NOISE:
            return
        if tag == "fr" or el.get("class") == "fontRun":
            cosmetic = True
        t = tok(el)
        strict.update(t)
        if cosmetic or tag in _HEAP_ROUTE:
            route.update(t)
        elif tag in _HEAP_GEOM:
            geom.extend(int(x) for x in re.findall(r"-?\d+", el.text or ""))
            loose.update(("<%s/>" % tag).encode())
        else:
            loose.update(t)
        for c in el:
            walk(c, cosmetic)
        strict.update(b"</>")
        if not cosmetic:
            loose.update(b"</>")

    walk(root, False)
    return {"strict": strict.hexdigest(), "loose": loose.hexdigest(), "route": route.hexdigest(), "geom": geom}


def _data_fingerprint(root):
    """DFDS (default values / data space): digest with compiler tables stripped."""
    import hashlib
    sect = root.find("DFDS/Section")
    if sect is None:
        return None
    items = []
    for df in sect:
        x = ET.tostring(df, encoding="unicode")
        if "nHiliteTableEntries" in x or "DSInit" in x or "<RepeatedBlock>" in x:
            continue
        x = re.sub(r' TypeID="\d+"', "", x)
        x = re.sub(r' VarType2="\d+"', "", x)
        x = re.sub(r"\s+", "", x)
        if re.fullmatch(r"<DataFill><I64>\d+</I64></DataFill>", x):
            continue
        items.append(x)
    if not items:
        return None   # separately compiled code / empty data space: cannot be compared
    return hashlib.sha1("\n".join(sorted(items)).encode()).hexdigest()


def _file_hash(work, patterns):
    import hashlib
    for pat in patterns:
        files = glob.glob(os.path.join(work, pat))
        if files:
            with open(files[0], "rb") as f:
                return hashlib.sha1(f.read()).hexdigest()
    return None


def _parse_conpane(fp_path, fp_map):
    """Connector pane: pattern id + terminal index -> control name"""
    if not fp_path or not os.path.exists(fp_path):
        return None
    root = _parse_xml(fp_path)
    cp = None
    for el in root.iter("conPane"):
        if len(el):
            cp = el
            break
    if cp is None:
        return None
    out = {"pattern": cp.findtext("conId"), "terms": {}}
    cons = cp.find("cons")
    if cons is not None:
        for c in cons:
            idx = c.get("index", "0")
            d = c.find("ConnectionDCO")
            uid = d.get("uid") if d is not None else None
            out["terms"][idx] = fp_map.get(uid, {}).get("name", "uid " + str(uid))
    return out


def parse_vi(vi_path, prim_names=None, keep_dir=None):
    """Parses a VI file and returns the model."""
    work = keep_dir or tempfile.mkdtemp(prefix="viver_")
    try:
        main_xml, warns = extract_with_pylabview(vi_path, work)
        root = _parse_xml(main_xml)
        model = {"parser_version": PARSER_VERSION, "warnings": warns[:20]}
        model["meta"] = _parse_meta(root)
        model["deps"] = _parse_deps(root)
        model["icon"] = _icon(work)
        types = TypeInfo(main_xml)
        fp_files = glob.glob(os.path.join(work, "vi_FPH*.xml"))
        fp_map = _parse_fp(fp_files[0]) if fp_files else {}
        model["fp"] = sorted(fp_map.values(), key=lambda c: (c["bounds"] or (0, 0))[:2])
        bd_files = glob.glob(os.path.join(work, "vi_BDH*.xml"))
        model["conpane"] = _parse_conpane(fp_files[0] if fp_files else None, fp_map)
        model["fingerprint"] = {
            "bd": _heap_fingerprint(bd_files[0] if bd_files else None),
            "fp": _heap_fingerprint(fp_files[0] if fp_files else None),
            "data": _data_fingerprint(root),
            "icon": _file_hash(work, ["*_icl8.png", "*_ICON.png"]),
        }
        if not bd_files:
            model["bd"] = None
            model["warnings"].append("Block diagram not found (the VI may be password protected, saved without a BD, or a .ctl).")
            return model
        bd_root = _parse_xml(bd_files[0])
        walker = BDWalker(bd_root, types, fp_map, _iuse_names(root), prim_names)
        walker.own_name = os.path.basename(model["meta"].get("file_name") or vi_path)
        diag_el = bd_root.find("root")
        if diag_el is None:
            raise VIParseError("BDHb has no root diagram")
        model["bd"] = walker.walk_diagram(diag_el, 0, 0)
        _fix_terminal_sides(model["bd"])
        model["bd_bounds"] = bd_bounds(model["bd"])
        model["stats"] = walker.stats
        # feed FP control/indicator info from the terminal guess
        io_by_fp = {}

        def collect_io(d):
            for n in d["nodes"]:
                if n["kind"] == "terminal" and n.get("fp_uid") and n.get("io"):
                    io_by_fp[n["fp_uid"]] = n["io"]
                for f in n.get("frames", []):
                    collect_io(f["diagram"])

        collect_io(model["bd"])
        for c in model["fp"]:
            c["io"] = "indicator" if c["flags"] & 0x1 else "control"
            c["on_bd"] = c["uid"] in io_by_fp
        return model
    finally:
        if keep_dir is None:
            shutil.rmtree(work, ignore_errors=True)


def is_vi_file(path):
    return path.lower().endswith(VI_EXTENSIONS)


if __name__ == "__main__":
    m = parse_vi(sys.argv[1])
    print(json.dumps({k: v for k, v in m.items() if k != "icon"}, indent=1, ensure_ascii=False)[:4000])
    print(m.get("stats"))
