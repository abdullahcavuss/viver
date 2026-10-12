"""Service that builds VI models and their diffs and caches them."""
import copy
import json
import os
import tempfile
import threading

from . import vi_parser, vi_diff

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


EMPTY_MODEL = {"meta": {}, "fp": [], "deps": [], "icon": None,
               "bd": {"uid": "root", "nodes": [], "wires": [], "notes": []},
               "bd_bounds": [0, 0, 400, 300], "warnings": [], "empty": True}


class VIService:
    def __init__(self, store):
        self.store = store
        self.model_dir = os.path.join(store.cache_dir, "models")
        self.diff_dir = os.path.join(store.cache_dir, "diffs")
        os.makedirs(self.model_dir, exist_ok=True)
        os.makedirs(self.diff_dir, exist_ok=True)

    def model(self, sha, filename="file.vi"):
        if not sha:
            return copy.deepcopy(EMPTY_MODEL)
        cache = os.path.join(self.model_dir, "%s.v%d.json" % (sha, vi_parser.PARSER_VERSION))
        with _lock_for(cache):
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    return json.load(f)
            ext = os.path.splitext(filename)[1] or ".vi"
            tmpdir = tempfile.mkdtemp(prefix="viver_m_")
            try:
                path = os.path.join(tmpdir, "input" + ext)
                self.store.blob_to_file(sha, path)
                try:
                    model = vi_parser.parse_vi(path)
                except vi_parser.VIParseError as e:
                    model = copy.deepcopy(EMPTY_MODEL)
                    model["error"] = str(e)
                except Exception as e:  # unexpected parse error
                    model = copy.deepcopy(EMPTY_MODEL)
                    model["error"] = "Parse error: %s: %s" % (type(e).__name__, e)
                model["sha"] = sha
                tmp = cache + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(model, f, ensure_ascii=False)
                os.replace(tmp, cache)
                return model
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

    def diff(self, old_sha, new_sha, filename="file.vi"):
        key = "%s_%s.v%d.json" % (old_sha or "none", new_sha or "none", vi_parser.PARSER_VERSION)
        cache = os.path.join(self.diff_dir, key)
        with _lock_for(cache):
            if os.path.exists(cache):
                with open(cache, encoding="utf-8") as f:
                    return json.load(f)
        old = self.model(old_sha, filename)
        new = self.model(new_sha, filename)
        res = vi_diff.diff_models(old, new)
        out = {"old": old, "new": new, "diff": res}
        with _lock_for(cache):
            tmp = cache + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(tmp, cache)
        return out

    def summary(self, old_sha, new_sha, filename="file.vi"):
        d = self.diff(old_sha, new_sha, filename)
        r = d["diff"]
        errs = [m.get("error") for m in (d["old"], d["new"]) if m.get("error")]
        v = r.get("verdict") or {"level": "functional", "parts": []}
        names = self.store.prim_names()
        if names:
            import copy, re
            v = copy.deepcopy(v)
            for p in v["parts"]:
                p["detail"] = re.sub(r"Prim (\d+)", lambda m: names.get(m.group(1), m.group(0)), p.get("detail") or "")
        return {"counts": r["counts"], "functional": r["functional"], "fp": len(r["fp"]),
                "deps": len(r["deps"]), "props": len([p for p in r["props"] if p["important"]]),
                "errors": errs, "level": v["level"], "parts": v["parts"], "cats": v.get("cats", [])}
