"""
VIVER — web-based version control for LabVIEW projects.

Run:        python app.py            (default http://127.0.0.1:5000)
Settings:   VIVER_DATA=<data folder>  VIVER_HOST=0.0.0.0  VIVER_PORT=5000
"""
import difflib
import io
import json
import os
import re
import shutil
import sys
import zipfile

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, url_for, Response)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from viver.storage import (Store, StorageError, diff_trees, build_tree_listing, human_size,  # noqa: E402
                           is_vi, is_text, file_kind, normalize_path)
from viver.service import VIService  # noqa: E402

DATA_DIR = os.environ.get("VIVER_DATA", os.path.join(HERE, "data"))

app = Flask(__name__)
app.secret_key = os.environ.get("VIVER_SECRET", "viver-local-secret")
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

store = Store(DATA_DIR)
vis = VIService(store)

app.jinja_env.filters["hsize"] = human_size
app.jinja_env.globals["file_kind"] = file_kind
app.jinja_env.globals["is_vi"] = is_vi


def get_repo_or_404(name):
    try:
        return store.get_repo(name)
    except StorageError:
        abort(404)


def ref_or_none(repo, ref):
    try:
        return store.get_commit(repo, ref)
    except StorageError as e:
        flash(str(e), "error")
        return None


@app.errorhandler(StorageError)
def handle_storage_error(e):
    if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json" \
            or request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": str(e)}), 400
    flash(str(e), "error")
    return redirect(request.referrer or url_for("index"))


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return render_template("base.html", repo=None), 404


PASSWORD = os.environ.get("VIVER_PASSWORD")


@app.before_request
def require_password():
    """If VIVER_PASSWORD is set, every request requires an HTTP Basic password (any user name)."""
    if not PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.password == PASSWORD:
        return None
    return Response("Password required", 401, {"WWW-Authenticate": 'Basic realm="VIVER"'})


@app.context_processor
def inject_globals():
    return {"app_name": "VIVER"}


# ------------------------------------------------------------------ home page
@app.route("/")
def index():
    repos = store.list_repos()
    usage, objects = store.disk_usage()
    return render_template("index.html", repos=repos, usage=usage, objects=objects)


@app.route("/new", methods=["POST"])
def create_repo():
    repo = store.create_repo(request.form.get("name", ""), request.form.get("description", ""),
                             request.form.get("work_dir", ""))
    flash("Repository “%s” created." % repo["name"], "ok")
    return redirect(url_for("repo_home", name=repo["name"]))


# ------------------------------------------------------------------ repository
@app.route("/r/<name>/")
def repo_home(name):
    repo = get_repo_or_404(name)
    ref = request.args.get("ref") or repo["current_branch"]
    path = request.args.get("path", "").strip("/")
    commit = ref_or_none(repo, ref)
    dirs, files = build_tree_listing(commit["tree"], path) if commit else ([], [])
    readme = None
    if commit and not path:
        for f in files:
            if f["name"].lower() in ("readme.md", "readme.txt"):
                readme = store.get_blob(f["sha"]).decode("utf-8", "replace")
                break
    # the commit in which each file last changed (fast for small repositories)
    last_change = {}
    if commit:
        history = store.log(repo, commit["id"], limit=400)
        for i, c in enumerate(history):
            parent_tree = history[i + 1]["tree"] if i + 1 < len(history) else {}
            for p, v in c["tree"].items():
                if p in last_change:
                    continue
                pv = parent_tree.get(p)
                if pv is None or pv[0] != v[0]:
                    last_change[p] = c
        for d in dirs:
            best = None
            for p, c in last_change.items():
                if p.startswith(d["path"] + "/") and (best is None or c["created_at"] > best["created_at"]):
                    best = c
            d["last"] = best
        for f in files:
            f["last"] = last_change.get(f["path"])
    crumbs = []
    acc = []
    for part in [p for p in path.split("/") if p]:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    total_commits = len(store.log(repo, ref, limit=100000)) if commit else 0
    return render_template("repo.html", repo=repo, commit=commit, ref=ref, path=path, dirs=dirs, files=files,
                           crumbs=crumbs, branches=store.branches(repo), tags=store.tags(repo), readme=readme,
                           total_commits=total_commits)


@app.route("/r/<name>/status")
def repo_status(name):
    repo = get_repo_or_404(name)
    if not repo["work_dir"]:
        flash("First set this repository's project folder in Settings, or commit by uploading files.", "info")
        return redirect(url_for("repo_upload", name=name))
    status = store.workdir_status(repo)
    return render_template("status.html", repo=repo, status=status, branches=store.branches(repo))


@app.route("/r/<name>/commit-workdir", methods=["POST"])
def commit_workdir(name):
    repo = get_repo_or_404(name)
    msg = request.form.get("message", "")
    desc = request.form.get("description", "").strip()
    if desc:
        msg = msg.strip() + "\n\n" + desc
    only = request.form.getlist("paths") or None
    if request.form.get("all") == "1":
        only = None
    c = store.commit_workdir(repo, msg, request.form.get("author", ""), only=only)
    flash("Commit %s created, project backed up." % c["short"] + _zip_note(c), "ok" if not c.get("zip_error") else "error")
    return redirect(url_for("commit_view", name=name, cid=c["id"]))


@app.route("/r/<name>/upload", methods=["GET", "POST"])
def repo_upload(name):
    repo = get_repo_or_404(name)
    if request.method == "GET":
        return render_template("upload.html", repo=repo, branches=store.branches(repo))
    files = {}
    mode = request.form.get("mode", "folder")
    if mode == "zip":
        up = request.files.get("zipfile")
        if not up:
            raise StorageError("No ZIP file selected.")
        z = zipfile.ZipFile(io.BytesIO(up.read()))
        names = [n for n in z.namelist() if not n.endswith("/")]
        # strip the common root folder
        roots = {n.split("/")[0] for n in names}
        strip = len(roots) == 1 and all("/" in n for n in names)
        for n in names:
            rel = n.split("/", 1)[1] if strip else n
            files[rel] = z.read(n)
    else:
        rels = request.form.getlist("relpath")
        ups = request.files.getlist("files")
        if not ups:
            raise StorageError("No file selected.")
        for i, f in enumerate(ups):
            rel = rels[i] if i < len(rels) else f.filename
            rel = rel.replace("\\", "/")
            if request.form.get("strip_root") == "1" and "/" in rel:
                rel = rel.split("/", 1)[1]
            files[rel] = f.read()
    rules = store.ignore_rules(repo)
    files = {normalize_path(k): v for k, v in files.items() if not store.is_ignored(normalize_path(k), rules)}
    if request.form.get("merge") == "1":
        head = store.branch_head(repo, request.form.get("branch") or repo["current_branch"])
        if head:
            base = dict(store.get_commit(repo, head)["tree"])
            merged = {k: tuple(v) for k, v in base.items()}
            merged.update(files)
            files = merged
    msg = request.form.get("message", "")
    desc = request.form.get("description", "").strip()
    if desc:
        msg = msg.strip() + "\n\n" + desc
    c = store.commit_files(repo, files, msg, request.form.get("author", ""),
                           branch=request.form.get("branch") or None)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "commit": c["id"], "url": url_for("commit_view", name=name, cid=c["id"])})
    flash("Commit %s created." % c["short"] + _zip_note(c), "ok" if not c.get("zip_error") else "error")
    return redirect(url_for("commit_view", name=name, cid=c["id"]))


def _zip_note(c):
    if c.get("zip"):
        return " Project ZIP: %s" % c["zip"]
    if c.get("zip_error"):
        return " WARNING: the commit is stored, but the project ZIP could not be written (%s)." % c["zip_error"]
    return ""


@app.route("/r/<name>/commits")
def commits(name):
    repo = get_repo_or_404(name)
    ref = request.args.get("ref") or repo["current_branch"]
    path = request.args.get("path")
    log = store.log(repo, ref, limit=500, path=path) if store.resolve_ref(repo, ref) else []
    tags_by_commit = {}
    for t in store.tags(repo):
        tags_by_commit.setdefault(t["commit_id"], []).append(t["name"])
    groups = []
    for c in log:
        day = c["time"].split(" ")[0]
        if not groups or groups[-1]["day"] != day:
            groups.append({"day": day, "commits": []})
        groups[-1]["commits"].append(c)
    return render_template("commits.html", repo=repo, ref=ref, path=path, groups=groups, count=len(log),
                           branches=store.branches(repo), tags_by_commit=tags_by_commit)


def _changes_with_meta(old_tree, new_tree):
    changes = diff_trees(old_tree, new_tree)
    for c in changes:
        c["is_vi"] = is_vi(c["path"])
    return changes


@app.route("/r/<name>/commit/<cid>")
def commit_view(name, cid):
    repo = get_repo_or_404(name)
    c = store.get_commit(repo, cid)
    parent = store.get_commit(repo, c["parent"]) if c["parent"] else None
    changes = _changes_with_meta(parent["tree"] if parent else {}, c["tree"])
    tags = [t for t in store.tags(repo) if t["commit_id"] == c["id"]]
    return render_template("compare.html", repo=repo, base=parent, head=c, changes=changes, single=True,
                           tags=tags, branches=store.branches(repo), export_dest=store.default_export_dir(repo, c))


@app.route("/r/<name>/compare")
def compare(name):
    repo = get_repo_or_404(name)
    base_ref = request.args.get("base")
    head_ref = request.args.get("head") or repo["current_branch"]
    log = store.log(repo, repo["current_branch"], limit=300)
    if not base_ref:
        return render_template("compare_pick.html", repo=repo, log=log, branches=store.branches(repo),
                               tags=store.tags(repo))
    base = store.get_commit(repo, base_ref)
    head = store.get_commit(repo, head_ref)
    if not base or not head:
        raise StorageError("No commit found to compare.")
    changes = _changes_with_meta(base["tree"], head["tree"])
    return render_template("compare.html", repo=repo, base=base, head=head, changes=changes, single=False,
                           tags=[], branches=store.branches(repo))


def _file_at(repo, ref, path):
    c = store.get_commit(repo, ref)
    if c is None or path not in c["tree"]:
        return c, None
    return c, c["tree"][path]


@app.route("/r/<name>/file")
def file_view(name):
    repo = get_repo_or_404(name)
    ref = request.args.get("ref") or repo["current_branch"]
    path = request.args.get("path", "")
    c, entry = _file_at(repo, ref, path)
    if entry is None:
        abort(404)
    sha, size = entry
    history = store.log(repo, c["id"], limit=200, path=path)
    content = None
    if not is_vi(path):
        data = store.get_blob(sha)
        if is_text(path, data):
            content = data.decode("utf-8", "replace")
    crumbs = []
    acc = []
    for part in path.split("/")[:-1]:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return render_template("file.html", repo=repo, commit=c, ref=ref, path=path, sha=sha, size=size,
                           history=history, content=content, crumbs=crumbs, fname=path.split("/")[-1],
                           kind=file_kind(path))


@app.route("/r/<name>/vidiff")
def vidiff_view(name):
    repo = get_repo_or_404(name)
    path = request.args.get("path", "")
    old_path = request.args.get("old_path") or path
    a = request.args.get("a")
    b = request.args.get("b")
    ca = store.get_commit(repo, a) if a else None
    cb = store.get_commit(repo, b) if b else None
    old_sha = ca["tree"].get(old_path, [None])[0] if ca else None
    new_sha = cb["tree"].get(path, [None])[0] if cb else None
    old_custom = new_custom = False
    old_label = "%s · %s" % (ca["short"], ca["title"][:50]) if ca else "(no file)"
    new_label = "%s · %s" % (cb["short"], cb["title"][:50]) if cb else "(no file)"
    if request.args.get("old_sha"):
        old_sha, old_custom = request.args["old_sha"], True
        old_label = request.args.get("old_label", "custom")
    if request.args.get("new_sha"):
        new_sha, new_custom = request.args["new_sha"], True
        new_label = request.args.get("new_label", "custom")
    history = store.log(repo, repo["current_branch"], limit=200, path=path) if store.resolve_ref(repo, repo["current_branch"]) else []
    if cb and not new_custom:
        back_url, back_label = url_for("commit_view", name=name, cid=cb["id"]), "Commit " + cb["short"]
    elif new_custom:
        back_url, back_label = url_for("repo_status", name=name), "Changes"
    else:
        back_url, back_label = url_for("repo_home", name=name), repo["name"]
    return render_template("vidiff.html", repo=repo, path=path, old_path=old_path, a=ca, b=cb,
                           old_sha=old_sha, new_sha=new_sha, history=history, fname=path.split("/")[-1],
                           old_label=old_label, new_label=new_label, old_custom=old_custom, new_custom=new_custom,
                           back_url=back_url, back_label=back_label)


@app.route("/r/<name>/wdiff")
def workdir_diff(name):
    """Compare a not-yet-committed VI with the last commit."""
    repo = get_repo_or_404(name)
    path = normalize_path(request.args.get("path", ""))
    full = os.path.join(repo["work_dir"], *path.split("/"))
    new_sha = None
    if os.path.exists(full):
        with open(full, "rb") as f:
            new_sha = store.put_blob(f.read())
    head = store.branch_head(repo, repo["current_branch"])
    hc = store.get_commit(repo, head) if head else None
    old_sha = hc["tree"].get(path, [None])[0] if hc else None
    args = {"name": name, "path": path, "new_label": "Working copy (uncommitted)"}
    if hc:
        args["a"] = hc["id"]
    if old_sha:
        args["old_sha"] = old_sha
        args["old_label"] = "HEAD %s · %s" % (hc["short"], hc["title"][:40])
    if new_sha:
        args["new_sha"] = new_sha
    return redirect(url_for("vidiff_view", **args))


@app.route("/r/<name>/textdiff")
def textdiff_view(name):
    repo = get_repo_or_404(name)
    path = request.args.get("path", "")
    old_path = request.args.get("old_path") or path
    ca = store.get_commit(repo, request.args.get("a")) if request.args.get("a") else None
    cb = store.get_commit(repo, request.args.get("b")) if request.args.get("b") else None
    old = store.get_blob(ca["tree"][old_path][0]).decode("utf-8", "replace") if ca and old_path in ca["tree"] else ""
    new = store.get_blob(cb["tree"][path][0]).decode("utf-8", "replace") if cb and path in cb["tree"] else ""
    return jsonify({"rows": text_diff_rows(old, new)})


def text_diff_rows(old, new, context=3):
    a = old.splitlines()
    b = new.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    rows = []
    for group in sm.get_grouped_opcodes(context):
        i1, j1 = group[0][1], group[0][3]
        i2, j2 = group[-1][2], group[-1][4]
        rows.append({"t": "hunk", "text": "@@ -%d,%d +%d,%d @@" % (i1 + 1, i2 - i1, j1 + 1, j2 - j1)})
        for tag, a1, a2, b1, b2 in group:
            if tag == "equal":
                for k in range(a2 - a1):
                    rows.append({"t": "eq", "o": a1 + k + 1, "n": b1 + k + 1, "text": a[a1 + k]})
            else:
                for k in range(a1, a2):
                    rows.append({"t": "del", "o": k + 1, "text": a[k]})
                for k in range(b1, b2):
                    rows.append({"t": "add", "n": k + 1, "text": b[k]})
    if len(rows) > 6000:
        rows = rows[:6000] + [{"t": "hunk", "text": "… diff too large, truncated"}]
    return rows


@app.route("/r/<name>/raw/<cid>/<path:path>")
def raw(name, cid, path):
    repo = get_repo_or_404(name)
    c, entry = _file_at(repo, cid, path)
    if entry is None:
        abort(404)
    data = store.get_blob(entry[0])
    return send_file(io.BytesIO(data), as_attachment=True, download_name=path.split("/")[-1])


@app.route("/r/<name>/zip/<ref>")
def download_zip(name, ref):
    repo = get_repo_or_404(name)
    buf, fname = store.zip_commit(repo, ref)
    return send_file(buf, as_attachment=True, download_name=fname, mimetype="application/zip")


@app.route("/r/<name>/restore", methods=["POST"])
def restore(name):
    repo = get_repo_or_404(name)
    ref = request.form.get("ref")
    paths = request.form.getlist("path") or None
    res = store.restore_to_workdir(repo, ref, paths)
    flash("Restored: %d file(s) written, %d file(s) deleted. The previous state was backed up: %s"
          % (res["written"], res["deleted"], res["backup"]), "ok")
    return redirect(url_for("repo_status", name=name))


@app.route("/r/<name>/export", methods=["POST"])
def export_folder(name):
    """Restore a commit into a separate folder; the project folder is not touched."""
    repo = get_repo_or_404(name)
    res = store.export_to_folder(repo, request.form.get("ref"), request.form.get("dest", ""))
    flash("Commit %s restored to %s (%d files, each verified against its checksum). The project folder was not touched."
          % (res["commit"], res["path"], res["files"]), "ok")
    return redirect(request.referrer or url_for("repo_home", name=name))


@app.route("/r/<name>/verify", methods=["POST"])
def verify_backups(name):
    repo = get_repo_or_404(name)
    r = store.verify_repo(repo)
    if r["missing"] or r["corrupt"]:
        flash("Backup check FAILED: %d missing and %d corrupted objects out of %d (in %d commits). First ones: %s"
              % (len(r["missing"]), len(r["corrupt"]), r["objects"], r["commits"],
                 ", ".join((r["missing"] + r["corrupt"])[:5])), "error")
    else:
        flash("Backup check passed: all %d stored file versions in %d commits are present and match their checksums."
              % (r["objects"], r["commits"]), "ok")
    return redirect(url_for("settings", name=name))


@app.route("/r/<name>/branches", methods=["GET", "POST"])
def branches(name):
    repo = get_repo_or_404(name)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            store.create_branch(repo, request.form.get("branch", ""), store.resolve_ref(repo, request.form.get("from") or repo["current_branch"]))
            flash("Branch created.", "ok")
        elif action == "delete":
            store.delete_branch(repo, request.form.get("branch"))
            flash("Branch deleted.", "ok")
        elif action == "checkout":
            res = store.checkout_branch(repo, request.form.get("branch"), restore=request.form.get("restore") == "1")
            msg = "Active branch: %s." % request.form.get("branch")
            if res:
                msg += " Project folder updated (%d written, %d deleted); the previous state was backed up." % (res["written"], res["deleted"])
            flash(msg, "ok")
        elif action == "tag":
            store.create_tag(repo, request.form.get("tag", ""), request.form.get("commit"), request.form.get("message", ""))
            flash("Version tag created.", "ok")
        elif action == "untag":
            store.delete_tag(repo, request.form.get("tag"))
            flash("Tag deleted.", "ok")
        return redirect(request.referrer or url_for("branches", name=name))
    brs = store.branches(repo)
    for b in brs:
        b["commit"] = store.get_commit(repo, b["head"]) if b["head"] else None
    return render_template("branches.html", repo=repo, branches=brs, tags=store.tags(repo))


@app.route("/r/<name>/settings", methods=["GET", "POST"])
def settings(name):
    repo = get_repo_or_404(name)
    if request.method == "POST":
        if request.form.get("action") == "delete":
            if request.form.get("confirm") == repo["name"]:
                store.delete_repo(repo)
                flash("Repository deleted (backup objects remain in the data folder).", "ok")
                return redirect(url_for("index"))
            raise StorageError("Type the repository name correctly to delete it.")
        f = request.form
        store.update_repo(repo, description=f.get("description", ""),
                          work_dir=f.get("work_dir", ""),
                          ignore_patterns=f.get("ignore_patterns", ""),
                          safe_read=1 if f.get("safe_read") else 0,
                          protect_workdir=1 if f.get("protect_workdir") else 0,
                          export_dir=f.get("export_dir", ""),
                          zip_on_commit=1 if f.get("zip_on_commit") else 0,
                          zip_dir=f.get("zip_dir", ""),
                          zip_keep=f.get("zip_keep") or 0)
        flash("Settings saved.", "ok")
        return redirect(url_for("settings", name=name))
    from viver.storage import DEFAULT_IGNORE
    return render_template("settings.html", repo=repo, default_ignore=DEFAULT_IGNORE, branches=store.branches(repo),
                           default_zip_dir=os.path.join(store.zip_root, repo["name"]))


# ------------------------------------------------------------------ primitive names (from the LabVIEW palettes)
import threading  # noqa: E402
from viver import palettes  # noqa: E402

PRIM_JOB = {"running": False, "done": 0, "total": 0, "added": 0, "found": 0, "path": None, "error": None, "finished": None}


def start_prim_import(lv_dir):
    if PRIM_JOB["running"]:
        return False

    def run():
        PRIM_JOB.update(running=True, done=0, total=0, added=0, found=0, path=lv_dir, error=None, finished=None)
        try:
            def prog(d, t):
                PRIM_JOB["done"], PRIM_JOB["total"] = d, t
            mapping, total = palettes.scan_installation(lv_dir, progress=prog)
            PRIM_JOB["found"] = len(mapping)
            PRIM_JOB["added"] = store.import_prim_names(mapping, "palette: " + os.path.basename(lv_dir))
            if total == 0:
                PRIM_JOB["error"] = "No menus/*.mnu palette files were found in this folder."
        except Exception as e:  # pragma: no cover
            PRIM_JOB["error"] = str(e)
        finally:
            PRIM_JOB["running"] = False
            PRIM_JOB["finished"] = fmt_now()

    threading.Thread(target=run, daemon=True).start()
    return True


def fmt_now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


@app.route("/primitives", methods=["GET", "POST"])
def primitives_page():
    if request.method == "POST":
        path = (request.form.get("lv_dir") or "").strip()
        if not path or not os.path.isdir(path):
            raise StorageError("LabVIEW folder not found: " + path)
        start_prim_import(path)
        return redirect(url_for("primitives_page"))
    rows = sorted(store.prim_name_rows(), key=lambda r: int(r["res_id"]) if r["res_id"].isdigit() else 0)
    return render_template("primitives.html", rows=rows, installs=palettes.find_installations(), job=PRIM_JOB)


@app.route("/api/prim-import")
def api_prim_import():
    return jsonify(PRIM_JOB)


# ------------------------------------------------------------------ JSON API
@app.route("/api/r/<name>/vimodel")
def api_vimodel(name):
    repo = get_repo_or_404(name)
    c, entry = _file_at(repo, request.args.get("ref"), request.args.get("path", ""))
    if entry is None:
        return jsonify({"error": "File not found"}), 404
    return Response(json.dumps(vis.model(entry[0], request.args.get("path")), ensure_ascii=False),
                    mimetype="application/json")


@app.route("/api/vidiff")
def api_vidiff():
    old = request.args.get("old") or None
    new = request.args.get("new") or None
    for s in (old, new):
        if s and (len(s) != 64 or not all(ch in "0123456789abcdef" for ch in s)):
            return jsonify({"error": "invalid hash"}), 400
    d = vis.diff(old, new, request.args.get("name", "x.vi"))
    return Response(json.dumps(d, ensure_ascii=False), mimetype="application/json")


@app.route("/api/visummary")
def api_visummary():
    old = request.args.get("old") or None
    new = request.args.get("new") or None
    try:
        return jsonify(vis.summary(old, new, request.args.get("name", "x.vi")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/prim-names", methods=["GET", "POST"])
def api_prim_names():
    if request.method == "POST":
        data = request.get_json(force=True)
        store.set_prim_name(data.get("id"), data.get("name"))
    return jsonify(store.prim_names())


@app.route("/api/r/<name>/status")
def api_status(name):
    repo = get_repo_or_404(name)
    return jsonify(store.workdir_status(repo))


@app.route("/api/r/<name>/commit", methods=["POST"])
def api_commit(name):
    """Commit from the command line / LabVIEW: {"message": "...", "author": "..."}"""
    repo = get_repo_or_404(name)
    data = request.get_json(force=True, silent=True) or request.form
    c = store.commit_workdir(repo, data.get("message", ""), data.get("author", ""))
    return jsonify({"ok": True, "id": c["id"], "short": c["short"], "stats": c["stats"],
                    "url": url_for("commit_view", name=name, cid=c["id"], _external=True)})


@app.route("/api/repos")
def api_repos():
    return jsonify([{"name": r["name"], "work_dir": r["work_dir"], "branch": r["current_branch"],
                     "commits": r["commit_count"]} for r in store.list_repos()])


# ------------------------------------------------------------------ CHANGELOG.md
from viver import changelog as changelog_mod, mdlite  # noqa: E402
import json as _json  # noqa: E402

CL_DIR = os.path.join(DATA_DIR, "changelogs")
CL_JOBS = {}


def _cl_path(jid):
    if not re.match(r"^[A-Za-z0-9_-]+$", jid or ""):
        abort(404)
    return os.path.join(CL_DIR, jid)


def _cl_meta(jid):
    p = os.path.join(_cl_path(jid), "job.json")
    if jid in CL_JOBS:
        return CL_JOBS[jid]
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return _json.load(f)
    return None


def start_changelog(repo, base, head, all_tiles, cats=None):
    import datetime
    jid = "%s-%s-%s" % (base["short"] if base else "initial", head["short"], datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
    out = _cl_path(jid)
    os.makedirs(out, exist_ok=True)
    meta = {"id": jid, "repo": repo["name"], "base": base["id"] if base else None, "head": head["id"],
            "base_short": base["short"] if base else None, "head_short": head["short"],
            "head_title": head["title"], "running": True, "done": 0, "total": 0, "current": "",
            "error": None, "result": None, "created": fmt_now()}
    CL_JOBS[jid] = meta

    def run():
        try:
            def prog(d, t, cur):
                meta.update(done=d, total=t, current=cur)
            meta["result"] = changelog_mod.generate(store, vis, repo, base["id"] if base else None, head["id"], out,
                                                    progress=prog, all_tiles=all_tiles, cats=cats)
        except Exception as e:
            import traceback
            meta["error"] = "%s: %s" % (type(e).__name__, e)
            traceback.print_exc()
        finally:
            meta["running"] = False
            with open(os.path.join(out, "job.json"), "w", encoding="utf-8") as f:
                _json.dump(meta, f, ensure_ascii=False)

    threading.Thread(target=run, daemon=True).start()
    return jid


@app.route("/r/<name>/changelog", methods=["GET", "POST"])
def changelog_page(name):
    repo = get_repo_or_404(name)
    if request.method == "POST":
        head = store.get_commit(repo, request.form.get("head") or repo["current_branch"])
        bref = request.form.get("base") or ""
        base = store.get_commit(repo, bref) if bref else None
        if head is None:
            raise StorageError("New version not found.")
        cats = request.form.getlist("cat") or list(changelog_mod.DEFAULT_CATS)
        jid = start_changelog(repo, base, head, request.form.get("all_tiles") == "1", cats)
        return redirect(url_for("changelog_view", name=name, jid=jid))
    log = store.log(repo, repo["current_branch"], limit=300) if store.resolve_ref(repo, repo["current_branch"]) else []
    previous = []
    if os.path.isdir(CL_DIR):
        for jid in sorted(os.listdir(CL_DIR), reverse=True):
            m = _cl_meta(jid)
            if m and m.get("repo") == name:
                previous.append(m)
    from viver.vi_diff import CATEGORIES
    return render_template("changelog.html", repo=repo, log=log, tags=store.tags(repo), previous=previous[:20],
                           base=request.args.get("base"), head=request.args.get("head"),
                           categories=CATEGORIES, default_cats=changelog_mod.DEFAULT_CATS)


@app.route("/r/<name>/changelog/<jid>")
def changelog_view(name, jid):
    repo = get_repo_or_404(name)
    meta = _cl_meta(jid)
    if meta is None:
        abort(404)
    body = None
    md_path = os.path.join(_cl_path(jid), "CHANGELOG.md")
    if not meta.get("running") and os.path.exists(md_path):
        with open(md_path, encoding="utf-8") as f:
            body = mdlite.render(f.read(), url_for("changelog_file", name=name, jid=jid, fpath="") )
    return render_template("changelog_view.html", repo=repo, meta=meta, body=body)


@app.route("/r/<name>/changelog/<jid>/f/<path:fpath>")
def changelog_file(name, jid, fpath):
    base = os.path.realpath(_cl_path(jid))
    full = os.path.realpath(os.path.join(base, fpath))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        abort(404)
    if full.lower().endswith(".svg"):   # the Windows registry may report the wrong type for .svg
        return send_file(full, mimetype="image/svg+xml")
    return send_file(full)


@app.route("/r/<name>/changelog/<jid>/zip")
def changelog_zip(name, jid):
    out = _cl_path(jid)
    if not os.path.exists(os.path.join(out, "CHANGELOG.md")):
        abort(404)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(out, "CHANGELOG.md"), "CHANGELOG.md")
        img = os.path.join(out, changelog_mod.IMG_DIR)
        if os.path.isdir(img):
            for f in sorted(os.listdir(img)):
                z.write(os.path.join(img, f), changelog_mod.IMG_DIR + "/" + f)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="CHANGELOG-%s.zip" % jid, mimetype="application/zip")


@app.route("/r/<name>/changelog/<jid>/md")
def changelog_md(name, jid):
    p = os.path.join(_cl_path(jid), "CHANGELOG.md")
    if not os.path.exists(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name="CHANGELOG.md", mimetype="text/markdown")


@app.route("/r/<name>/changelog/<jid>/save", methods=["POST"])
def changelog_save(name, jid):
    """Copies CHANGELOG.md and its images into the project folder."""
    repo = get_repo_or_404(name)
    if not repo["work_dir"]:
        raise StorageError("No project folder is set for this repository.")
    if repo.get("protect_workdir"):
        raise StorageError("This repository is set to never write to the project folder; download the ZIP instead.")
    out = _cl_path(jid)
    shutil.copy(os.path.join(out, "CHANGELOG.md"), os.path.join(repo["work_dir"], "CHANGELOG.md"))
    dst = os.path.join(repo["work_dir"], changelog_mod.IMG_DIR)
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    src = os.path.join(out, changelog_mod.IMG_DIR)
    if os.path.isdir(src):
        shutil.copytree(src, dst)
    flash("CHANGELOG.md and the %s folder were saved to the project folder." % changelog_mod.IMG_DIR, "ok")
    return redirect(url_for("changelog_view", name=name, jid=jid))


@app.route("/api/changelog/<jid>")
def api_changelog(jid):
    m = _cl_meta(jid)
    if m is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(m)


@app.route("/api/r/<name>/changelog", methods=["POST"])
def api_changelog_start(name):
    """From the command line: {"base": "...", "head": "..."}"""
    repo = get_repo_or_404(name)
    data = request.get_json(force=True, silent=True) or {}
    head = store.get_commit(repo, data.get("head") or repo["current_branch"])
    base = store.get_commit(repo, data["base"]) if data.get("base") else None
    jid = start_changelog(repo, base, head, bool(data.get("all_tiles")), data.get("cats"))
    return jsonify({"id": jid, "status": url_for("api_changelog", jid=jid, _external=True),
                    "zip": url_for("changelog_zip", name=name, jid=jid, _external=True)})


def auto_import_prims():
    """On first run: if there are no palette names at all, import them from the newest LabVIEW installation found."""
    try:
        if any(r["source"] != "user" for r in store.prim_name_rows()):
            return
        inst = palettes.find_installations()
        if inst:
            start_prim_import(inst[0])
    except Exception:
        pass


if __name__ == "__main__":
    auto_import_prims()
    host = os.environ.get("VIVER_HOST", "127.0.0.1")
    port = int(os.environ.get("VIVER_PORT", "5000"))
    print("VIVER is running:  http://%s:%d   (data: %s)" % (host if host != "0.0.0.0" else "localhost", port, DATA_DIR))
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, debug=False, threaded=True)
