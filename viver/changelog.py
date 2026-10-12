"""
Generates CHANGELOG.md between two versions (with images).

Output folder:
    CHANGELOG.md
    changelog-images/<vi>_v1.svg ...   (previous + new version in the same SVG)
"""
import datetime
import difflib
import os
import re
import shutil
import unicodedata
import zipfile

from .storage import diff_trees, is_vi, is_text, human_size
from . import render, svgrender
from .vi_diff import CATEGORIES, CHANGE_CATEGORY, change_cat

DEFAULT_CATS = ("logic", "fp")

IMG_DIR = "changelog-images"

KIND_TXT = {
    "added": "Added", "removed": "Removed", "modified": "Modified",
    "wire_added": "Wire added", "wire_removed": "Wire removed", "wire_modified": "Wire changed",
    "note_added": "Comment added", "note_removed": "Comment removed",
}
KIND_MARK = {"added": "🟩", "wire_added": "🟩", "note_added": "🟩",
             "removed": "🟥", "wire_removed": "🟥", "note_removed": "🟥",
             "modified": "🟧", "wire_modified": "🟧", "moved": "🟦", "reframed": "🟪"}
FILE_TXT = {"added": "added", "deleted": "deleted", "modified": "modified", "renamed": "moved/renamed"}


def _ascii(text):
    """Transliterate accented letters to ASCII (dotless i is not decomposed by NFKD)."""
    text = text.replace("\u0131", "i").replace("\u0130", "I")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slug(path):
    path = _ascii(path)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", path.replace("/", "__"))
    s = re.sub(r"\.(vi|ctl|vit|vim|ctt)$", "", s, flags=re.I)
    return s.strip("_")[:80] or "vi"


def anchor(path):
    return "vi-" + slug(path).lower().replace(".", "-")


def _short(t, n=60):
    t = str(t)
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def _brief(item, names, cats, max_items=3):
    """Summarizes the changes in one VI on a single line."""
    c, res = item["c"], item["d"]["diff"]
    if c["status"] == "added":
        n = len((item["d"]["new"].get("bd") or {}).get("nodes") or [])
        return "New VI added" + (" (%d top-level items on the block diagram)" % n if n else "")
    if c["status"] == "deleted":
        return "VI deleted"
    bits = []
    changes = [ch for ch in res["changes"] if change_cat(ch) in cats]
    logic = [ch for ch in changes if change_cat(ch) == "logic"]
    if logic:
        cnt = {}
        for ch in logic:
            k = "reframed" if ch.get("reframe") else ch["kind"].replace("wire_", "").replace("note_", "")
            cnt[k] = cnt.get(k, 0) + 1
        marks = " ".join("%s%d" % (KIND_MARK.get(k, "•"), v) for k, v in sorted(cnt.items()))
        pri = {"modified": 0, "added": 1, "removed": 2}
        ranked = sorted(logic, key=lambda ch: (bool(ch.get("reframe")), ch["kind"].startswith(("wire_", "note_")),
                                                pri.get(ch["kind"].split("_")[-1], 3)))
        titles = []
        for ch in ranked[:max_items]:
            t = _short(_prim_sub(ch["title"], names))
            if ch.get("detail") and ch["kind"] == "modified":
                t += " (%s)" % _short(_prim_sub(ch["detail"], names), 40)
            titles.append(t)
        more = len(logic) - len(titles)
        bits.append("**Block diagram** %s: %s%s" % (marks, "; ".join(titles), (" (+%d more)" % more) if more else ""))
    moved = [ch for ch in changes if ch["kind"] == "moved"]
    if moved:
        bits.append("Block diagram layout: %d item(s) moved/resized" % len(moved))
    for p in item["parts"]:
        if p["key"] in ("bd", "version", "bd_notes") or (p["key"] == "bd_layout" and moved):
            continue
        det = _short(_prim_sub(p.get("detail") or "", names), 70)
        bits.append("%s%s" % (p["label"], (": " + det) if det else ""))
    return " · ".join(bits) or "Details below"


def num_ranges(nums):
    """[1,2,3,5,7,8] -> '1–3, 5, 7–8'"""
    nums = sorted(set(nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else "%d–%d" % (nums[i], nums[j]))
        i = j + 1
    return ", ".join(out)


def md_escape(t):
    return str(t).replace("|", "\\|").replace("\n", " ")


def commits_between(store, repo, base_id, head_id):
    out = []
    cid = head_id
    while cid and cid != base_id and len(out) < 500:
        c = store.get_commit(repo, cid)
        if c is None:
            break
        out.append(c)
        cid = c["parent"]
    return out


def _prim_sub(text, names):
    return re.sub(r"Prim (\d+)", lambda m: names.get(m.group(1), m.group(0)), text or "")


def generate(store, vis, repo, base_ref, head_ref, out_dir, progress=None, all_tiles=False, cats=None):
    cats = set(cats or DEFAULT_CATS)
    head = store.get_commit(repo, head_ref)
    if head is None:
        raise ValueError("New version not found")
    base = store.get_commit(repo, base_ref) if base_ref else None
    if base_ref and base is None:
        raise ValueError("Old version not found")
    os.makedirs(os.path.join(out_dir, IMG_DIR), exist_ok=True)
    names = store.prim_names()
    changes = diff_trees(base["tree"] if base else {}, head["tree"])
    vi_changes = [c for c in changes if is_vi(c["path"]) and c["status"] in ("added", "deleted", "modified")]
    other = [c for c in changes if c not in vi_changes]
    total = len(vi_changes)

    functional, layout, saved, errors, excluded = [], [], [], [], []
    for i, c in enumerate(vi_changes, 1):
        if progress:
            progress(i - 1, total, c["path"])
        d = vis.diff(c.get("old_sha"), c.get("new_sha"), c["path"])
        verdict = d["diff"].get("verdict") or {"level": "functional", "parts": [], "cats": ["logic"]}
        sel = [p for p in verdict["parts"] if p.get("cat", "other") in cats or p["key"] in ("error", "file")]
        item = {"c": c, "d": d, "verdict": verdict, "parts": sel,
                "base_short": base["short"] if base else None, "head_short": head["short"]}
        if any(p["key"] == "error" for p in verdict["parts"]):
            errors.append(item)
        elif verdict["level"] == "saved":
            (saved if "saved" in cats else excluded).append(item)
        elif not [p for p in sel if p["key"] != "version"]:
            excluded.append(item)
        elif any(p["level"] == "functional" for p in sel):
            functional.append(item)
        else:
            layout.append(item)
    if progress:
        progress(total, total, "")

    L = []
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    L.append("# Changelog — %s" % repo["name"])
    L.append("")
    if base:
        L.append("**Range:** `%s` %s (%s) → `%s` %s (%s)  " % (base["short"], md_escape(base["title"]), base["time"],
                                                             head["short"], md_escape(head["title"]), head["time"]))
    else:
        L.append("**Version:** `%s` %s (%s) — first version  " % (head["short"], md_escape(head["title"]), head["time"]))
    L.append("**Branch:** `%s` · **Generated:** %s · VIVER  " % (head["branch"], now))
    L.append("**Scope:** %s  " % ", ".join(t for k, t in CATEGORIES if k in cats))
    L.append("")

    # ---- summary
    L.append("## Summary")
    L.append("")
    L.append("| | Count |")
    L.append("|---|---:|")
    L.append("| VIs with content changes | %d |" % len(functional))
    L.append("| VIs with visual/layout changes only | %d |" % len(layout))
    if "saved" in cats:
        L.append("| VIs re-saved only | %d |" % len(saved))
    if excluded:
        L.append("| VIs out of scope | %d |" % len(excluded))
    if errors:
        L.append("| Unreadable VIs | %d |" % len(errors))
    L.append("| Other file changes | %d |" % len(other))
    L.append("")

    # ---- short summary of the changed VIs (details and images below)
    if functional or layout:
        L.append("### Changed VIs")
        L.append("")
        L.append("| VI | Status | What changed |")
        L.append("|---|---|---|")
        for grp, item in [("content", it) for it in functional] + [("layout", it) for it in layout]:
            c = item["c"]
            name = c["path"].split("/")[-1]
            folder = "/".join(c["path"].split("/")[:-1])
            st = {"added": "🟩 New", "deleted": "🟥 Deleted", "modified": "🟧 Modified"}[c["status"]]
            if grp == "layout":
                st += " (layout)"
            L.append("| [%s](#%s)%s | %s | %s |" % (md_escape(name), anchor(c["path"]),
                                                  ("<br>`%s`" % md_escape(folder)) if folder else "",
                                                  st, md_escape(_brief(item, names, cats))))
        L.append("")
        L.append("_Details and images are below; click a VI name to jump to its section._")
        L.append("")

    # ---- commits
    cl = commits_between(store, repo, base["id"] if base else None, head["id"])
    if cl:
        L.append("## Commits")
        L.append("")
        for c in cl:
            L.append("- `%s` %s — **%s** _(%s)_" % (c["short"], c["time"], md_escape(c["title"]), c["author"] or "?"))
            if c["body"]:
                for line in c["body"].splitlines():
                    L.append("  > %s" % line)
        L.append("")

    # ---- VI sections
    legend = ("Changed items are marked with numbered boxes in the images: 🟩 added, 🟥 removed, 🟧 modified, "
              "🟪 only moved into another structure/frame%s. "
              "Unchanged parts are dimmed. The numbers match the lists below."
              % (", 🟦 moved" if "bd_layout" in cats else ""))
    for heading, group in (("VIs with content changes", functional), ("Visual/layout changes", layout)):
        if not group:
            continue
        L.append("## %s" % heading)
        L.append("")
        L.append(legend)
        L.append("")
        for item in group:
            if progress:
                progress(total, total, "images: " + item["c"]["path"])
            L.extend(_vi_section(item, out_dir, names, all_tiles, cats))

    if errors:
        L.append("## Unreadable VIs")
        L.append("")
        for item in errors:
            msg = "; ".join(p.get("detail") or p["label"] for p in item["verdict"]["parts"] if p["key"] == "error")
            L.append("- `%s` — %s" % (item["c"]["path"], md_escape(msg)))
        L.append("")

    if excluded:
        L.append("## VIs out of scope")
        L.append("")
        L.append("<details><summary>%d VI(s) — changes only in categories that were not selected</summary>" % len(excluded))
        L.append("")
        for item in excluded:
            labels = sorted({p["label"] for p in item["verdict"]["parts"] if p["key"] != "version"}) or ["re-saved"]
            L.append("- `%s` — %s" % (item["c"]["path"], ", ".join(labels)))
        L.append("")
        L.append("</details>")
        L.append("")

    if saved:
        L.append("## VIs re-saved only")
        L.append("")
        L.append("<details><summary>%d VI(s) — content unchanged (revision / compiled code / internal numbers)</summary>" % len(saved))
        L.append("")
        for item in saved:
            ver = next((p["detail"] for p in item["verdict"]["parts"] if p["key"] == "version"), "")
            L.append("- `%s`%s" % (item["c"]["path"], (" — LabVIEW " + ver) if ver else ""))
        L.append("")
        L.append("</details>")
        L.append("")

    # ---- other files
    if other:
        L.append("## Other files")
        L.append("")
        for c in other:
            if c["status"] == "renamed":
                L.append("- `%s` → `%s` (%s)" % (c["old_path"], c["path"], FILE_TXT["renamed"]))
                continue
            size = human_size(c["new_size"] or c["old_size"] or 0)
            L.append("- `%s` — %s (%s)" % (c["path"], FILE_TXT.get(c["status"], c["status"]), size))
            if c["status"] == "modified" and is_text(c["path"]):
                try:
                    a = store.get_blob(c["old_sha"]).decode("utf-8", "replace").splitlines()
                    b = store.get_blob(c["new_sha"]).decode("utf-8", "replace").splitlines()
                    dl = list(difflib.unified_diff(a, b, lineterm="", n=1))[2:]
                    if dl:
                        L.append("")
                        L.append("  ```diff")
                        for line in dl[:60]:
                            L.append("  " + line)
                        if len(dl) > 60:
                            L.append("  … (%d more lines)" % (len(dl) - 60))
                        L.append("  ```")
                except Exception:
                    pass
        L.append("")

    md = "\n".join(L).rstrip() + "\n"
    with open(os.path.join(out_dir, "CHANGELOG.md"), "w", encoding="utf-8") as f:
        f.write(md)
    return {"functional": len(functional), "layout": len(layout), "saved": len(saved),
            "errors": len(errors), "other": len(other), "commits": len(cl), "excluded": len(excluded),
            "cats": sorted(cats)}


def _vi_section(item, out_dir, names, all_tiles, cats):
    c, d, verdict = item["c"], item["d"], item["verdict"]
    res = d["diff"]
    L = []
    title = c["path"].split("/")[-1]
    L.append('<a id="%s"></a>' % anchor(c["path"]))
    L.append("")
    L.append("### %s" % title)
    L.append("")
    status = {"added": "New VI", "deleted": "Deleted VI", "modified": "Modified"}[c["status"]]
    L.append("`%s` · %s" % (c["path"], status))
    L.append("")
    shown = [p for p in item["parts"] if p["key"] not in ("bd", "version", "bd_notes")
             and not (p["key"] == "bd_layout" and any(ch["kind"] == "moved" for ch in res["changes"]))]
    for p in shown:
        L.append("- **%s**%s" % (p["label"], (": " + md_escape(_prim_sub(p["detail"], names))) if p.get("detail") else ""))
    if shown:
        L.append("")

    changes = [ch for ch in res["changes"] if change_cat(ch) in cats]
    order = {"logic": 0, "auto": 1, "bd_layout": 2}
    changes.sort(key=lambda ch: (order.get(change_cat(ch), 0), bool(ch.get("reframe"))))  # frame-only moves last
    if c["status"] != "modified":
        changes = []  # in a new/deleted VI every item is "added/removed"; the image is enough instead of a list
    if changes:
        L.append("**Block diagram changes**" if any(change_cat(ch) == "logic" for ch in changes)
                 else "**Block diagram layout changes**")
        L.append("")
        for i, ch in enumerate(changes, 1):
            t = _prim_sub(ch["title"], names)
            line = "%d. %s %s" % (i, KIND_MARK.get("reframed" if ch.get("reframe") else ch["kind"], "•"), md_escape(t))
            if ch.get("detail"):
                line += " — %s" % md_escape(_prim_sub(ch["detail"], names))
            if ch.get("path"):
                line += " _(%s)_" % md_escape(ch["path"])
            L.append(line)
        L.append("")

    # images
    hn, ho, rn, ro = {}, {}, [], []
    for i, ch in enumerate(changes, 1):
        k = ch["kind"]
        if ch.get("new_uid") and k == "moved":
            hn[ch["new_uid"]] = ("moved", i)
            rn.append((i, ch.get("new_reveal") or []))
        if ch.get("new_uid") and k in ("added", "modified", "wire_added", "wire_modified", "note_added"):
            hn[ch["new_uid"]] = ("added" if "added" in k else ("reframed" if ch.get("reframe") else "modified"), i)
            rn.append((i, ch.get("new_reveal") or []))
        if ch.get("old_uid") and k in ("removed", "modified", "wire_removed", "wire_modified", "note_removed"):
            ho[ch["old_uid"]] = ("removed" if "removed" in k else ("reframed" if ch.get("reframe") else "modified"), i)
            ro.append((i, ch.get("old_reveal") or []))
    base = os.path.join(out_dir, IMG_DIR, slug(c["path"]))
    new_bd = d["new"].get("bd") if not d["new"].get("empty") else None
    old_bd = d["old"].get("bd") if not d["old"].get("empty") else None

    old_t = "Previous version" + (" (%s)" % item["base_short"] if item.get("base_short") else "")
    new_t = "New version" + (" (%s)" % item["head_short"] if item.get("head_short") else "")
    if c["status"] == "added":
        old_bd, ho, ro = None, {}, []
    elif c["status"] == "deleted":
        new_bd, hn, rn = None, {}, []
    has_old = bool(old_bd and old_bd.get("nodes"))
    has_new = bool(new_bd and new_bd.get("nodes"))
    if (has_old or has_new) and not (c["status"] == "modified" and not (rn or ro)):
        try:
            views = svgrender.render_pair(old_bd if has_old else None, ho, ro, new_bd if has_new else None, hn, rn,
                                          base, names, old_title=old_t, new_title=new_t)
        except Exception as e:  # an image failure for one VI must not stop the whole CHANGELOG
            import logging
            logging.exception("Could not render CHANGELOG image: %s", c["path"])
            L.append("> ⚠ Could not render an image for this VI (%s: %s). The list of changes is above." % (type(e).__name__, e))
            L.append("")
            views = []
        if views:
            L.append("**Block diagram — previous and new version**")
            L.append("")
        for vi, v in enumerate(views, 1):
            if len(views) > 1 or v["desc"]:
                L.append("_View %d%s%s_" % (vi, (" — " + "; ".join(v["desc"])) if v["desc"] else "",
                                              (" · change " + num_ranges(v["nums"])) if v["nums"] else ""))
                L.append("")
            rel = os.path.relpath(v["file"], out_dir).replace(os.sep, "/").replace(" ", "%20")
            L.append("[![%s — previous and new version](%s)](%s)" % (md_escape(title), rel, rel))
            L.append("")
        if views:
            L.append("_The image is an SVG: click to open it full size; it stays sharp when you zoom in the browser._")
            L.append("")
    L.append("---")
    L.append("")
    return L


def zip_dir(out_dir, zip_path, root_name="CHANGELOG"):
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for r, _, files in os.walk(out_dir):
            for f in files:
                full = os.path.join(r, f)
                if full == zip_path:
                    continue
                z.write(full, os.path.join(root_name, os.path.relpath(full, out_dir)))
