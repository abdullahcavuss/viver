"""
VIVER storage layer.

A Git-like content-addressed store:
  data/objects/ab/cdef...   -> zlib-compressed file contents (SHA-256)
  data/viver.db             -> repositories, commits, branches, tags (SQLite)

Every commit stores the complete state of the project at that moment (file path -> content hash).
Because identical content is stored only once, every commit is a full backup, yet disk
usage grows only by the files that changed.
"""
import fnmatch
import hashlib
import io
import json
import shutil
import os
import re
import sqlite3
import threading
import time
import zipfile
import zlib
from datetime import datetime

DEFAULT_IGNORE = [
    ".git/", ".svn/", ".hg/", ".viver/",
    "*.lvlps", "*.aliases", "*.lvbitx.tmp",
    "builds/", "Builds/",
    "*.bak", "*.tmp", "~$*", "Thumbs.db", ".DS_Store", "desktop.ini",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    work_dir TEXT DEFAULT '',
    current_branch TEXT DEFAULT 'main',
    ignore_patterns TEXT DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commits (
    id TEXT PRIMARY KEY,
    repo_id INTEGER NOT NULL,
    parent TEXT,
    branch TEXT NOT NULL,
    message TEXT NOT NULL,
    author TEXT DEFAULT '',
    created_at REAL NOT NULL,
    tree TEXT NOT NULL,
    stats TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_commits_repo ON commits(repo_id, created_at);
CREATE TABLE IF NOT EXISTS branches (
    repo_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    head TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (repo_id, name)
);
CREATE TABLE IF NOT EXISTS tags (
    repo_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    commit_id TEXT NOT NULL,
    message TEXT DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (repo_id, name)
);
CREATE TABLE IF NOT EXISTS prim_names (
    res_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
"""


class StorageError(Exception):
    pass


# Per-repository safety/backup options (added to older databases on start-up)
REPO_OPTIONS = {
    "safe_read": "INTEGER DEFAULT 1",        # read project files only through a verified snapshot copy
    "protect_workdir": "INTEGER DEFAULT 0",  # never write into the project folder
    "export_dir": "TEXT DEFAULT ''",         # default target for "restore to another folder"
    "zip_on_commit": "INTEGER DEFAULT 0",    # write a ZIP of the whole project on every commit
    "zip_dir": "TEXT DEFAULT ''",            # where those ZIPs go ('' = data/commit-zips/<repo>)
    "zip_keep": "INTEGER DEFAULT 0",         # keep only the newest N ZIPs (0 = keep all)
}


def _now():
    return time.time()


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _is_inside(path, folder):
    path, folder = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


class FileBusy(Exception):
    pass


def normalize_path(p):
    p = p.replace("\\", "/").strip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise StorageError("Invalid path: " + p)
    return "/".join(parts)


class Store:
    def __init__(self, data_dir):
        self.data_dir = os.path.abspath(data_dir)
        self.obj_dir = os.path.join(self.data_dir, "objects")
        self.cache_dir = os.path.join(self.data_dir, "cache")
        self.backup_dir = os.path.join(self.data_dir, "restore-backups")
        self.snap_dir = os.path.join(self.data_dir, "snapshot-tmp")
        self.zip_root = os.path.join(self.data_dir, "commit-zips")
        for d in (self.obj_dir, self.cache_dir, self.backup_dir, self.snap_dir):
            os.makedirs(d, exist_ok=True)
        self.db_path = os.path.join(self.data_dir, "viver.db")
        self._local = threading.local()
        self._hash_cache = {}  # (path, size, mtime) -> sha
        self._lock = threading.Lock()
        with self.db() as c:
            c.executescript(SCHEMA)
            cols = [r[1] for r in c.execute("PRAGMA table_info(prim_names)")]
            if "source" not in cols:  # for older databases
                c.execute("ALTER TABLE prim_names ADD COLUMN source TEXT DEFAULT 'user'")
            rcols = [r[1] for r in c.execute("PRAGMA table_info(repos)")]
            for col, decl in REPO_OPTIONS.items():
                if col not in rcols:
                    c.execute("ALTER TABLE repos ADD COLUMN %s %s" % (col, decl))
            # older versions labelled palette imports with a localized prefix
            c.execute("UPDATE prim_names SET source = 'palette: ' || substr(source, 8) WHERE source LIKE 'palet: %'")
            c.commit()

    # ------------------------------------------------------------ db
    def db(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ objects
    def _obj_path(self, sha):
        return os.path.join(self.obj_dir, sha[:2], sha[2:])

    def put_blob(self, data):
        sha = hashlib.sha256(data).hexdigest()
        path = self._obj_path(sha)
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp%d" % os.getpid()
            with open(tmp, "wb") as f:
                f.write(zlib.compress(data, 6))
            os.replace(tmp, path)
        return sha

    def get_blob(self, sha, verify=False):
        path = self._obj_path(sha)
        if not os.path.exists(path):
            raise StorageError("Object not found: " + sha)
        with open(path, "rb") as f:
            try:
                data = zlib.decompress(f.read())
            except zlib.error:
                raise StorageError("Backup object is corrupted (cannot be decompressed): " + sha)
        if verify and hashlib.sha256(data).hexdigest() != sha:
            raise StorageError("Backup object is corrupted (checksum mismatch): " + sha)
        return data

    def blob_to_file(self, sha, dest):
        """Writes a stored file to disk: verified source, temp file, read-back check, then atomic replace."""
        data = self.get_blob(sha, verify=True)
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        tmp = dest + ".viver-tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != sha:
                os.remove(tmp)
                raise StorageError("Written file failed verification: " + dest)
        os.replace(tmp, dest)

    # ------------------------------------------------------------ repos
    def create_repo(self, name, description="", work_dir="", ignore_patterns=""):
        name = name.strip()
        if not re.match(r"^[A-Za-z0-9._\-]{1,80}$", name):
            raise StorageError("A repository name may only contain letters, digits, dots, hyphens and underscores.")
        if work_dir:
            work_dir = os.path.abspath(os.path.expanduser(work_dir.strip()))
            if not os.path.isdir(work_dir):
                raise StorageError("Folder not found: " + work_dir)
        c = self.db()
        try:
            cur = c.execute("INSERT INTO repos(name, description, work_dir, ignore_patterns, created_at) VALUES (?,?,?,?,?)",
                            (name, description, work_dir, ignore_patterns, _now()))
            rid = cur.lastrowid
            c.execute("INSERT INTO branches(repo_id, name, head, created_at) VALUES (?,?,?,?)",
                      (rid, "main", None, _now()))
            c.commit()
        except sqlite3.IntegrityError:
            raise StorageError("A repository with this name already exists.")
        return self.get_repo(name)

    def update_repo(self, repo, **fields):
        allowed = {"description", "work_dir", "ignore_patterns", "current_branch"} | set(REPO_OPTIONS)
        sets, vals = [], []
        work_dir = fields.get("work_dir", repo.get("work_dir")) or ""
        if work_dir:
            work_dir = os.path.abspath(os.path.expanduser(work_dir.strip()))
        for k, v in fields.items():
            if k in allowed:
                if k == "work_dir" and v:
                    v = os.path.abspath(os.path.expanduser(v.strip()))
                    if not os.path.isdir(v):
                        raise StorageError("Folder not found: " + v)
                if k in ("zip_dir", "export_dir"):
                    v = (v or "").strip()
                    if v:
                        v = os.path.abspath(os.path.expanduser(v))
                        if work_dir and _is_inside(v, work_dir):
                            raise StorageError("This folder is inside the project folder; choose a folder outside it "
                                               "(otherwise its contents would be committed): " + v)
                if k in ("safe_read", "protect_workdir", "zip_on_commit", "zip_keep"):
                    try:
                        v = max(0, int(v or 0))
                    except ValueError:
                        raise StorageError("Invalid number for %s." % k)
                sets.append(k + "=?")
                vals.append(v)
        if sets:
            vals.append(repo["id"])
            self.db().execute("UPDATE repos SET " + ",".join(sets) + " WHERE id=?", vals)
            self.db().commit()

    def delete_repo(self, repo):
        c = self.db()
        for t in ("commits", "branches", "tags"):
            c.execute("DELETE FROM %s WHERE repo_id=?" % t, (repo["id"],))
        c.execute("DELETE FROM repos WHERE id=?", (repo["id"],))
        c.commit()

    def list_repos(self):
        rows = self.db().execute("SELECT * FROM repos ORDER BY name").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            last = self.db().execute("SELECT * FROM commits WHERE repo_id=? ORDER BY created_at DESC LIMIT 1",
                                     (r["id"],)).fetchone()
            d["last_commit"] = self._commit_dict(last) if last else None
            d["commit_count"] = self.db().execute("SELECT COUNT(*) FROM commits WHERE repo_id=?", (r["id"],)).fetchone()[0]
            out.append(d)
        return out

    def get_repo(self, name):
        r = self.db().execute("SELECT * FROM repos WHERE name=?", (name,)).fetchone()
        if r is None:
            raise StorageError("Repository not found: " + name)
        return dict(r)

    # ------------------------------------------------------------ branches & tags
    def branches(self, repo):
        rows = self.db().execute("SELECT * FROM branches WHERE repo_id=? ORDER BY name", (repo["id"],)).fetchall()
        return [dict(r) for r in rows]

    def branch_head(self, repo, branch):
        r = self.db().execute("SELECT head FROM branches WHERE repo_id=? AND name=?", (repo["id"], branch)).fetchone()
        if r is None:
            raise StorageError("Branch not found: " + branch)
        return r["head"]

    def create_branch(self, repo, name, from_commit=None):
        name = name.strip()
        if not re.match(r"^[A-Za-z0-9._\-/]{1,80}$", name):
            raise StorageError("Invalid branch name.")
        if from_commit:
            self.get_commit(repo, from_commit)
        try:
            self.db().execute("INSERT INTO branches(repo_id, name, head, created_at) VALUES (?,?,?,?)",
                              (repo["id"], name, from_commit, _now()))
            self.db().commit()
        except sqlite3.IntegrityError:
            raise StorageError("This branch already exists.")

    def delete_branch(self, repo, name):
        if name == repo["current_branch"]:
            raise StorageError("The active branch cannot be deleted.")
        self.db().execute("DELETE FROM branches WHERE repo_id=? AND name=?", (repo["id"], name))
        self.db().commit()

    def tags(self, repo):
        rows = self.db().execute("SELECT * FROM tags WHERE repo_id=? ORDER BY created_at DESC", (repo["id"],)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["time"] = fmt_time(d["created_at"])
            out.append(d)
        return out

    def create_tag(self, repo, name, commit_id, message=""):
        name = name.strip()
        if not re.match(r"^[A-Za-z0-9._\-]{1,80}$", name):
            raise StorageError("Invalid tag name.")
        c = self.get_commit(repo, commit_id)
        try:
            self.db().execute("INSERT INTO tags(repo_id, name, commit_id, message, created_at) VALUES (?,?,?,?,?)",
                              (repo["id"], name, c["id"], message, _now()))
            self.db().commit()
        except sqlite3.IntegrityError:
            raise StorageError("This tag already exists.")

    def delete_tag(self, repo, name):
        self.db().execute("DELETE FROM tags WHERE repo_id=? AND name=?", (repo["id"], name))
        self.db().commit()

    # ------------------------------------------------------------ commits
    def _commit_dict(self, row):
        if row is None:
            return None
        d = dict(row)
        d["tree"] = json.loads(d["tree"])
        d["stats"] = json.loads(d.get("stats") or "{}")
        d["short"] = d["id"][:7]
        d["time"] = fmt_time(d["created_at"])
        d["title"] = d["message"].strip().split("\n")[0]
        d["body"] = "\n".join(d["message"].strip().split("\n")[1:]).strip()
        return d

    def resolve_ref(self, repo, ref):
        """Branch name, tag, full or short commit id -> commit id (or None: empty branch)"""
        if not ref:
            ref = repo["current_branch"]
        r = self.db().execute("SELECT head FROM branches WHERE repo_id=? AND name=?", (repo["id"], ref)).fetchone()
        if r is not None:
            return r["head"]
        r = self.db().execute("SELECT commit_id FROM tags WHERE repo_id=? AND name=?", (repo["id"], ref)).fetchone()
        if r is not None:
            return r["commit_id"]
        rows = self.db().execute("SELECT id FROM commits WHERE repo_id=? AND id LIKE ?", (repo["id"], ref + "%")).fetchall()
        if len(rows) == 1:
            return rows[0]["id"]
        if len(rows) > 1:
            raise StorageError("Ambiguous commit abbreviation: " + ref)
        raise StorageError("Not found: " + ref)

    def get_commit(self, repo, ref):
        cid = self.resolve_ref(repo, ref)
        if cid is None:
            return None
        row = self.db().execute("SELECT * FROM commits WHERE repo_id=? AND id=?", (repo["id"], cid)).fetchone()
        if row is None:
            raise StorageError("Commit not found: " + ref)
        return self._commit_dict(row)

    def log(self, repo, ref=None, limit=200, path=None):
        """History going back from a branch/commit. If path is given, only the commits that changed that file."""
        cid = self.resolve_ref(repo, ref)
        out = []
        prev_sha = None
        chain = []
        while cid:
            row = self.db().execute("SELECT * FROM commits WHERE repo_id=? AND id=?", (repo["id"], cid)).fetchone()
            if row is None:
                break
            chain.append(self._commit_dict(row))
            cid = row["parent"]
            if path is None and len(chain) >= limit:
                break
        if path is None:
            return chain
        # file history: commits where the content hash changed
        for i, c in enumerate(chain):
            cur = c["tree"].get(path)
            parent = chain[i + 1]["tree"].get(path) if i + 1 < len(chain) else None
            cur_sha = cur[0] if cur else None
            par_sha = parent[0] if parent else None
            if cur_sha != par_sha:
                c = dict(c)
                c["file_change"] = "added" if par_sha is None else ("deleted" if cur_sha is None else "modified")
                c["file_sha"] = cur_sha
                out.append(c)
            if len(out) >= limit:
                break
        return out

    def commit_files(self, repo, files, message, author="", branch=None, source="upload"):
        """files: {relative_path: bytes or ('sha', size)} -> new commit dict"""
        message = (message or "").strip()
        if not message:
            raise StorageError("The commit message cannot be empty.")
        branch = branch or repo["current_branch"]
        with self._lock:
            parent = self.branch_head(repo, branch)
            tree = {}
            for path, data in files.items():
                path = normalize_path(path)
                if not path:
                    continue
                if isinstance(data, tuple):
                    tree[path] = [data[0], data[1]]
                else:
                    tree[path] = [self.put_blob(data), len(data)]
            parent_c = self.get_commit(repo, parent) if parent else None
            if parent_c and parent_c["tree"] == tree:
                raise StorageError("No changes since the last commit.")
            changes = diff_trees(parent_c["tree"] if parent_c else {}, tree)
            stats = {
                "added": len([c for c in changes if c["status"] == "added"]),
                "modified": len([c for c in changes if c["status"] == "modified"]),
                "deleted": len([c for c in changes if c["status"] == "deleted"]),
                "renamed": len([c for c in changes if c["status"] == "renamed"]),
                "files": len(tree),
                "size": sum(v[1] for v in tree.values()),
                "vi_changed": len([c for c in changes if is_vi(c["path"])]),
                "source": source,
            }
            ts = _now()
            raw = json.dumps([parent, branch, message, author, ts, sorted(tree.items())]).encode()
            cid = hashlib.sha1(raw).hexdigest()
            c = self.db()
            c.execute("INSERT INTO commits(id, repo_id, parent, branch, message, author, created_at, tree, stats) VALUES (?,?,?,?,?,?,?,?,?)",
                      (cid, repo["id"], parent, branch, message, author, ts, json.dumps(tree), json.dumps(stats)))
            c.execute("UPDATE branches SET head=? WHERE repo_id=? AND name=?", (cid, repo["id"], branch))
            c.commit()
        commit = self.get_commit(repo, cid)
        if repo.get("zip_on_commit"):
            try:
                commit["zip"] = self.write_commit_zip(repo, commit)
            except Exception as e:  # the commit itself is already safely stored
                commit["zip_error"] = "%s: %s" % (type(e).__name__, e)
        return commit

    # ------------------------------------------------------------ working folder
    def ignore_rules(self, repo):
        rules = list(DEFAULT_IGNORE)
        rules += [l.strip() for l in (repo.get("ignore_patterns") or "").splitlines() if l.strip() and not l.startswith("#")]
        wd = repo.get("work_dir")
        if wd:
            p = os.path.join(wd, ".viverignore")
            if os.path.exists(p):
                with open(p, encoding="utf-8", errors="replace") as f:
                    rules += [l.strip() for l in f if l.strip() and not l.startswith("#")]
        return rules

    @staticmethod
    def is_ignored(relpath, rules):
        parts = relpath.split("/")
        name = parts[-1]
        for r in rules:
            if r.endswith("/"):
                d = r.rstrip("/")
                if d in parts[:-1] or fnmatch.fnmatch(relpath + "/", r) or any(fnmatch.fnmatch(p, d) for p in parts[:-1]):
                    return True
            elif "/" in r:
                if fnmatch.fnmatch(relpath, r.lstrip("/")):
                    return True
            elif fnmatch.fnmatch(name, r):
                return True
        return False

    def _hash_file(self, full):
        st = os.stat(full)
        key = (full, st.st_size, st.st_mtime_ns)
        sha = self._hash_cache.get(key)
        if sha is None:
            h = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()
            self._hash_cache[key] = sha
        return sha, st.st_size

    SNAPSHOT_QUIET = 2.0   # a file must have been unchanged for this many seconds before it is read

    def _snapshot_file(self, full, attempts=6):
        """Safe read: copy the project file to a private snapshot and read ONLY the copy.
        The copy is accepted only if the file had been quiet (unmodified) for SNAPSHOT_QUIET seconds
        before copying and did not change during the copy (size and time stamp before == after).
        The original is opened read-only, once, for the duration of the copy.
        Returns (sha, size). Raises FileBusy if the file keeps changing (e.g. LabVIEW is saving it)."""
        st = os.stat(full)
        key = (full, st.st_size, st.st_mtime_ns)
        sha = self._hash_cache.get(key)
        if sha is not None and os.path.exists(self._obj_path(sha)):
            return sha, st.st_size
        for i in range(attempts):
            st1 = os.stat(full)
            age = time.time() - st1.st_mtime
            if age < self.SNAPSHOT_QUIET:          # still being written (or just written): wait
                time.sleep(min(self.SNAPSHOT_QUIET - age + 0.1, 1.0))
                continue
            tmp = os.path.join(self.snap_dir, "%d-%d.snap" % (os.getpid(), threading.get_ident()))
            try:
                shutil.copyfile(full, tmp)
                st2 = os.stat(full)
                if (st1.st_size, st1.st_mtime_ns) == (st2.st_size, st2.st_mtime_ns) \
                        and os.path.getsize(tmp) == st2.st_size:
                    with open(tmp, "rb") as f:
                        data = f.read()
                    sha = self.put_blob(data)
                    self._hash_cache[(full, st2.st_size, st2.st_mtime_ns)] = sha
                    return sha, len(data)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            time.sleep(0.5)
        raise FileBusy(full)

    def scan_workdir(self, repo, busy=None):
        """Scans the project folder. With safe_read, every new/changed file is read through a verified
        snapshot copy and stored right away, so later steps never touch the original again.
        Files that keep changing while being read are appended to `busy` (if given) and left out."""
        wd = repo.get("work_dir")
        if not wd or not os.path.isdir(wd):
            raise StorageError("No working folder is set for this repository, or it cannot be found.")
        safe = bool(repo.get("safe_read", 1))
        rules = self.ignore_rules(repo)
        tree = {}
        for root, dirs, files in os.walk(wd):
            rel_root = os.path.relpath(root, wd).replace("\\", "/")
            rel_root = "" if rel_root == "." else rel_root
            dirs[:] = sorted(d for d in dirs if not self.is_ignored((rel_root + "/" + d).strip("/") + "/x", rules))
            for fn in sorted(files):
                rel = (rel_root + "/" + fn).strip("/")
                if self.is_ignored(rel, rules) or fn.endswith(".viver-tmp"):
                    continue
                full = os.path.join(root, fn)
                try:
                    tree[rel] = list(self._snapshot_file(full) if safe else self._hash_file(full))
                except FileBusy:
                    if busy is not None:
                        busy.append(rel)
                except OSError:
                    if busy is not None:
                        busy.append(rel)
        return tree

    def workdir_status(self, repo):
        busy = []
        tree = self.scan_workdir(repo, busy)
        head = self.branch_head(repo, repo["current_branch"])
        head_tree = self.get_commit(repo, head)["tree"] if head else {}
        for rel in busy:   # unreadable right now: keep the committed version so it does not look deleted
            if rel in head_tree:
                tree[rel] = head_tree[rel]
        changes = diff_trees(head_tree, tree)
        # Store the contents of uncommitted VIs in the object store so that diffs/summaries can be computed.
        # (Harmless because it is content-addressed; the same object is reused when committing.)
        for c in changes:
            if c.get("new_sha") and is_vi(c["path"]) and not os.path.exists(self._obj_path(c["new_sha"])):
                full = os.path.join(repo["work_dir"], *c["path"].split("/"))
                try:
                    with open(full, "rb") as f:
                        c["new_sha"] = self.put_blob(f.read())
                except OSError:
                    c["unreadable"] = True
        return {"head": head, "branch": repo["current_branch"], "changes": changes, "files": len(tree), "busy": busy}

    def commit_workdir(self, repo, message, author="", only=None):
        """Commits the whole working folder (or only the changes in the 'only' list)."""
        wd = repo["work_dir"]
        busy = []
        tree = self.scan_workdir(repo, busy)
        if busy and (only is None or set(busy) & set(only)):
            raise StorageError("These files were changing while VIVER was reading them (LabVIEW may still be saving), "
                               "so nothing was committed. Wait a moment and try again: " + ", ".join(busy[:10]))
        head = self.branch_head(repo, repo["current_branch"])
        head_tree = self.get_commit(repo, head)["tree"] if head else {}
        if only is not None:
            only = set(only)
            new_tree = dict(head_tree)
            for p in only:
                if p in tree:
                    new_tree[p] = tree[p]
                else:
                    new_tree.pop(p, None)
            tree = new_tree
        files = {}
        for rel, (sha, size) in tree.items():
            if not os.path.exists(self._obj_path(sha)):
                full = os.path.join(wd, *rel.split("/"))
                with open(full, "rb") as f:
                    data = f.read()
                real = self.put_blob(data)
                files[rel] = (real, len(data))
            else:
                files[rel] = (sha, size)
        return self.commit_files(repo, files, message, author, source="workdir")

    def restore_to_workdir(self, repo, ref, paths=None):
        try:
            return self._restore_to_workdir(repo, ref, paths)
        except PermissionError as e:
            raise StorageError("Could not write to the file (%s). The VI may be open in LabVIEW; close it in LabVIEW and try again. "
                               "The previous state was still backed up to the restore-backups folder." % getattr(e, "filename", e))

    def _restore_to_workdir(self, repo, ref, paths=None):
        """Restores a commit (or selected files of it) into the working folder.
        First takes a ZIP backup of the current state of the working folder."""
        wd = repo.get("work_dir")
        if not wd or not os.path.isdir(wd):
            raise StorageError("No working folder is set.")
        if repo.get("protect_workdir"):
            raise StorageError("This repository is set to never write to the project folder. "
                               "Use “Restore to another folder” instead (or change it in Settings).")
        commit = self.get_commit(repo, ref)
        if commit is None:
            raise StorageError("There is no commit to restore.")
        current = self.scan_workdir(repo)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(self.backup_dir, "%s-%s-before-%s.zip" % (repo["name"], stamp, commit["short"]))
        with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in current:
                if paths is None or rel in paths:
                    z.write(os.path.join(wd, *rel.split("/")), rel)
        written, deleted = 0, 0
        target = commit["tree"]
        if paths is not None:
            for rel in paths:
                full = os.path.join(wd, *rel.split("/"))
                if rel in target:
                    self.blob_to_file(target[rel][0], full)
                    written += 1
                elif os.path.exists(full):
                    os.remove(full)
                    deleted += 1
        else:
            for rel, (sha, size) in target.items():
                cur = current.get(rel)
                if cur and cur[0] == sha:
                    continue
                self.blob_to_file(sha, os.path.join(wd, *rel.split("/")))
                written += 1
            for rel in current:
                if rel not in target:
                    os.remove(os.path.join(wd, *rel.split("/")))
                    deleted += 1
        self._hash_cache.clear()
        return {"written": written, "deleted": deleted, "backup": backup}

    def checkout_branch(self, repo, branch, restore=True):
        head = self.branch_head(repo, branch)
        result = None
        if restore and repo.get("work_dir") and head and not repo.get("protect_workdir"):
            result = self.restore_to_workdir(repo, head)
        self.update_repo(repo, current_branch=branch)
        return result

    # ------------------------------------------------------------ export
    def zip_commit(self, repo, ref, prefix=None):
        commit = self.get_commit(repo, ref)
        buf = io.BytesIO()
        prefix = prefix or "%s-%s" % (repo["name"], commit["short"])
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, (sha, size) in sorted(commit["tree"].items()):
                z.writestr(prefix + "/" + rel, self.get_blob(sha, verify=True))
        buf.seek(0)
        return buf, prefix + ".zip"

    def default_export_dir(self, repo, commit):
        base = repo.get("export_dir") or ""
        if not base:
            wd = repo.get("work_dir")
            base = os.path.join(os.path.dirname(wd.rstrip("\\/")), repo["name"] + "-versions") if wd \
                else os.path.join(self.data_dir, "exports")
        return os.path.join(base, "%s-%s" % (repo["name"], commit["short"]))

    def export_to_folder(self, repo, ref, dest):
        """Restores a commit into a separate, new (or empty) folder; the project folder is never touched.
        Every file is verified against its checksum after writing."""
        commit = self.get_commit(repo, ref)
        if commit is None:
            raise StorageError("Commit not found: " + str(ref))
        dest = os.path.abspath(os.path.expanduser((dest or "").strip()))
        if not dest or dest == os.path.abspath(os.sep):
            raise StorageError("Choose a target folder.")
        wd = repo.get("work_dir")
        if wd and (_is_inside(dest, wd) or _is_inside(wd, dest)):
            raise StorageError("The target folder must be outside the project folder (and must not contain it).")
        if os.path.exists(dest) and (not os.path.isdir(dest) or os.listdir(dest)):
            raise StorageError("The target folder already exists and is not empty: " + dest)
        os.makedirs(dest, exist_ok=True)
        for rel, (sha, size) in sorted(commit["tree"].items()):
            self.blob_to_file(sha, os.path.join(dest, *rel.split("/")))
        return {"path": dest, "files": len(commit["tree"]), "commit": commit["short"]}

    def write_commit_zip(self, repo, commit):
        """ZIP of the whole project at this commit (built from the verified backup objects, not from the
        project folder). Written to a temporary name, tested, then renamed; old ZIPs pruned if configured."""
        folder = repo.get("zip_dir") or os.path.join(self.zip_root, repo["name"])
        os.makedirs(folder, exist_ok=True)
        stamp = datetime.fromtimestamp(commit["created_at"]).strftime("%Y%m%d-%H%M%S")
        name = "%s-%s-%s.zip" % (repo["name"], stamp, commit["short"])
        path = os.path.join(folder, name)
        tmp = path + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for rel, (sha, size) in sorted(commit["tree"].items()):
                z.writestr(rel, self.get_blob(sha, verify=True))
            z.writestr(".viver-commit.txt", "repository: %s\ncommit: %s\nbranch: %s\nauthor: %s\ndate: %s\n\n%s\n" % (
                repo["name"], commit["id"], commit["branch"], commit["author"], commit["time"], commit["message"]))
        with zipfile.ZipFile(tmp) as z:
            bad = z.testzip()
        if bad:
            os.remove(tmp)
            raise StorageError("The commit ZIP failed its integrity test (%s)." % bad)
        os.replace(tmp, path)
        keep = int(repo.get("zip_keep") or 0)
        if keep > 0:
            mine = sorted((f for f in os.listdir(folder) if f.startswith(repo["name"] + "-") and f.endswith(".zip")),
                          key=lambda f: os.path.getmtime(os.path.join(folder, f)), reverse=True)
            for f in mine[keep:]:
                try:
                    os.remove(os.path.join(folder, f))
                except OSError:
                    pass
        return path

    def verify_repo(self, repo):
        """Checks every backup object referenced by the repository's commits: present, decompressable,
        and matching its SHA-256."""
        shas = set()
        rows = self.db().execute("SELECT tree FROM commits WHERE repo_id=?", (repo["id"],)).fetchall()
        for r in rows:
            for sha, size in json.loads(r["tree"]).values():
                shas.add(sha)
        missing, corrupt = [], []
        for sha in sorted(shas):
            if not os.path.exists(self._obj_path(sha)):
                missing.append(sha)
                continue
            try:
                self.get_blob(sha, verify=True)
            except StorageError:
                corrupt.append(sha)
        return {"commits": len(rows), "objects": len(shas), "missing": missing, "corrupt": corrupt}

    # ------------------------------------------------------------ primitive names
    def prim_names(self):
        return {r["res_id"]: r["name"] for r in self.db().execute("SELECT * FROM prim_names")}

    def prim_name_rows(self):
        return [dict(r) for r in self.db().execute("SELECT res_id, name, COALESCE(source,'user') AS source FROM prim_names")]

    def set_prim_name(self, res_id, name, source="user"):
        res_id = str(res_id).strip()
        name = (name or "").strip()
        if not name:
            self.db().execute("DELETE FROM prim_names WHERE res_id=?", (res_id,))
        else:
            self.db().execute("INSERT OR REPLACE INTO prim_names(res_id, name, source) VALUES (?,?,?)",
                              (res_id, name[:60], source))
        self.db().commit()

    def import_prim_names(self, mapping, source):
        """Adds palette names; does not touch names the user entered manually."""
        c = self.db()
        user = {r["res_id"] for r in c.execute("SELECT res_id FROM prim_names WHERE COALESCE(source,'user')='user'")}
        n = 0
        for rid, name in mapping.items():
            if rid in user:
                continue
            c.execute("INSERT OR REPLACE INTO prim_names(res_id, name, source) VALUES (?,?,?)", (rid, name[:60], source))
            n += 1
        c.commit()
        return n

    def disk_usage(self):
        total = 0
        count = 0
        for root, _, files in os.walk(self.obj_dir):
            for f in files:
                total += os.path.getsize(os.path.join(root, f))
                count += 1
        return total, count


# ---------------------------------------------------------------- helpers

VI_EXT = (".vi", ".vit", ".ctl", ".ctt", ".vim")
TEXT_EXT = (".lvproj", ".lvlib", ".lvclass", ".xctl", ".lvsc", ".lvtest", ".xml", ".txt", ".md", ".ini",
            ".csv", ".json", ".py", ".m", ".c", ".h", ".cpp", ".cs", ".bat", ".sh", ".cfg", ".yml", ".yaml",
            ".html", ".css", ".js", ".lvproj", ".vipb", ".lvtst", ".gitignore", ".viverignore")


def is_vi(path):
    return path.lower().endswith(VI_EXT)


def is_text(path, data=None):
    if path.lower().endswith(TEXT_EXT):
        return True
    if data is not None and len(data) < 2_000_000:
        sample = data[:4096]
        if b"\x00" in sample:
            return False
        try:
            sample.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


def file_kind(path):
    p = path.lower()
    if p.endswith((".vi", ".vit", ".vim")):
        return "vi"
    if p.endswith((".ctl", ".ctt")):
        return "ctl"
    if p.endswith(".lvproj"):
        return "proj"
    if p.endswith((".lvlib", ".lvclass", ".xctl", ".lvsc")):
        return "lib"
    if p.endswith(".llb"):
        return "llb"
    return "file"


def diff_trees(old, new):
    out = []
    for p in sorted(set(old) | set(new)):
        a, b = old.get(p), new.get(p)
        if a and not b:
            out.append({"path": p, "status": "deleted", "old_sha": a[0], "new_sha": None, "old_size": a[1], "new_size": 0})
        elif b and not a:
            out.append({"path": p, "status": "added", "old_sha": None, "new_sha": b[0], "old_size": 0, "new_size": b[1]})
        elif a[0] != b[0]:
            out.append({"path": p, "status": "modified", "old_sha": a[0], "new_sha": b[0], "old_size": a[1], "new_size": b[1]})
    # rename detection: the same content was deleted and added elsewhere
    deleted = {c["old_sha"]: c for c in out if c["status"] == "deleted"}
    final = []
    used = set()
    for c in out:
        if c["status"] == "added" and c["new_sha"] in deleted and c["new_sha"] not in used:
            d = deleted[c["new_sha"]]
            used.add(c["new_sha"])
            final.append({"path": c["path"], "old_path": d["path"], "status": "renamed", "old_sha": d["old_sha"],
                          "new_sha": c["new_sha"], "old_size": d["old_size"], "new_size": c["new_size"]})
    for c in out:
        if c["status"] == "deleted" and c["old_sha"] in used:
            continue
        if c["status"] == "added" and c["new_sha"] in used and any(f.get("path") == c["path"] for f in final):
            continue
        final.append(c)
    final.sort(key=lambda c: c["path"])
    for c in final:
        c["kind"] = file_kind(c["path"])
    return final


def build_tree_listing(tree, path=""):
    """Extracts the contents of a single folder (folders + files) from a flat {path: [sha,size]} tree."""
    path = path.strip("/")
    prefix = path + "/" if path else ""
    dirs = {}
    files = []
    for p, (sha, size) in tree.items():
        if not p.startswith(prefix):
            continue
        rest = p[len(prefix):]
        if "/" in rest:
            d = rest.split("/")[0]
            info = dirs.setdefault(d, {"name": d, "path": prefix + d, "count": 0, "size": 0, "vis": 0})
            info["count"] += 1
            info["size"] += size
            if is_vi(p):
                info["vis"] += 1
        else:
            files.append({"name": rest, "path": p, "sha": sha, "size": size, "kind": file_kind(p)})
    return sorted(dirs.values(), key=lambda d: d["name"].lower()), sorted(files, key=lambda f: f["name"].lower())


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
