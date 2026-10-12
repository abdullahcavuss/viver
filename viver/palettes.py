"""
Reads primitive names from the function palettes (.mnu) of a LabVIEW installation.

Primitives are stored in a VI file only by their resource number (primResID); their names
live inside LabVIEW. Palette files, however, keep both the displayed name (CPST block)
and an item record (PLM2 block) for every item:

    PLM2:  [version:u16][item count:u16]
           each item: [id:u16][kind:u16][extra:u32][pascal string][pad to even address]

The id of "Function" records equals the primResID in the VI. Items are in the same order
as the names in CPST.
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from .vi_parser import THIRD_PARTY

DEFAULT_ROOTS = [
    r"C:\Program Files\National Instruments",
    r"C:\Program Files (x86)\National Instruments",
]


def find_installations(roots=None):
    """LabVIEW installations on this computer (those with a menus folder), newest first."""
    out = []
    for root in roots or DEFAULT_ROOTS:
        if not os.path.isdir(root):
            continue
        for d in sorted(os.listdir(root), reverse=True):
            full = os.path.join(root, d)
            if d.lower().startswith("labview") and os.path.isdir(os.path.join(full, "menus")):
                out.append(full)
    return out


def parse_plm2(data):
    """PLM2 block -> [(id, kind, extra, text)]"""
    if len(data) < 4:
        return []
    count = int.from_bytes(data[2:4], "big")
    pos = 4
    items = []
    for _ in range(count):
        if pos + 9 > len(data):
            break
        ident = int.from_bytes(data[pos:pos + 2], "big")
        kind = int.from_bytes(data[pos + 2:pos + 4], "big")
        extra = int.from_bytes(data[pos + 4:pos + 8], "big")
        n = data[pos + 8]
        text = data[pos + 9:pos + 9 + n].decode("latin-1", "replace")
        pos += 9 + n
        if pos % 2:
            pos += 1
        items.append((ident, kind, extra, text))
    return items


def read_palette(mnu_path, work_dir):
    """Extracts [(primResID, name)] from a .mnu file."""
    env = dict(os.environ)
    env["PYTHONPATH"] = THIRD_PARTY + os.pathsep + env.get("PYTHONPATH", "")
    base = os.path.join(work_dir, "m")
    for f in glob.glob(base + "*"):
        os.remove(f)
    subprocess.run([sys.executable, "-W", "ignore", "-m", "pylabview.readRSRC", "-x", "-i", mnu_path,
                    "-m", base + ".xml"], cwd=work_dir, env=env, capture_output=True, timeout=60)
    if not os.path.exists(base + ".xml"):
        return []
    root = ET.parse(base + ".xml").getroot()
    names = [s.text or "" for s in root.findall("CPST/Section/String")]
    plm = None
    sect = root.find("PLM2/Section")
    if sect is not None and sect.get("File"):
        p = os.path.join(work_dir, sect.get("File"))
        if os.path.exists(p):
            with open(p, "rb") as f:
                plm = f.read()
    if plm is None:
        return []
    out = []
    for i, (ident, kind, extra, text) in enumerate(parse_plm2(plm)):
        if text != "Function" or i >= len(names):
            continue
        name = names[i].strip()
        if not name or name.lower().endswith((".vi", ".vim", ".mnu", ".ctl", ".xnode")):
            continue
        out.append((ident, name))
    return out


def scan_installation(lv_dir, progress=None, workers=4):
    """Scans all palettes of a LabVIEW installation: ({resID(str): name}, file count)"""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    files = sorted(glob.glob(os.path.join(lv_dir, "menus", "**", "*.mnu"), recursive=True))
    result = {}
    done = [0]
    lock = threading.Lock()
    local = threading.local()
    dirs = []

    def job(f):
        if not hasattr(local, "work"):
            local.work = tempfile.mkdtemp(prefix="viver_mnu_")
            with lock:
                dirs.append(local.work)
        try:
            items = read_palette(f, local.work)
        except Exception:
            items = []
        with lock:
            for ident, name in items:
                result.setdefault(str(ident), name)
            done[0] += 1
            if progress:
                progress(done[0], len(files))

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(job, files))
    finally:
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)
    return result, len(files)
