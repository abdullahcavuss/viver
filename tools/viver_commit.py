#!/usr/bin/env python3
"""
VIVER command line client — sends commit/status requests to a running VIVER server.

Examples:
  python tools/viver_commit.py Line1 -m "Calibration updated" -a Alice
  python tools/viver_commit.py Line1 --status
  python tools/viver_commit.py --list

To call it from LabVIEW with System Exec.vi:
  python "C:\\viver\\tools\\viver_commit.py" Line1 -m "Automatic backup before build" -a LabVIEW

Uses only the Python standard library.
"""
import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = os.environ.get("VIVER_URL", "http://127.0.0.1:5000")


def call(server, path, data=None):
    url = server.rstrip("/") + path
    body = json.dumps(data).encode("utf-8") if data is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    pw = os.environ.get("VIVER_PASSWORD")
    if pw:
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(("cli:" + pw).encode()).decode()
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            msg = str(e)
        raise SystemExit("Error: %s" % msg)
    except urllib.error.URLError as e:
        raise SystemExit("Could not reach the VIVER server (%s): %s" % (server, e.reason))


def main():
    ap = argparse.ArgumentParser(description="VIVER commit client")
    ap.add_argument("repo", nargs="?", help="Repository name")
    ap.add_argument("-m", "--message", help="Commit message")
    ap.add_argument("-a", "--author", default=os.environ.get("USERNAME") or os.environ.get("USER") or "", help="Author name")
    ap.add_argument("--status", action="store_true", help="List uncommitted changes")
    ap.add_argument("--list", action="store_true", help="List repositories")
    ap.add_argument("--changelog", metavar="OLD..NEW",
                    help="Generate and download a CHANGELOG.md (+images) ZIP. E.g.: v1.0..main  or  ..main (from the first version)")
    ap.add_argument("-o", "--output", help="ZIP path to save for --changelog")
    ap.add_argument("--scope", default="logic,fp",
                    help="--changelog categories: logic,bd_layout,fp,auto,other,saved (default %(default)s)")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="Server address (default %(default)s)")
    args = ap.parse_args()

    if args.list:
        for r in call(args.server, "/api/repos"):
            print("%-24s %-10s %4d commit  %s" % (r["name"], r["branch"], r["commits"], r["work_dir"]))
        return
    if not args.repo:
        ap.error("repository name required")
    name = urllib.parse.quote(args.repo)
    if args.changelog:
        import time
        base, _, head = args.changelog.partition("..")
        job = call(args.server, "/api/r/%s/changelog" % name, {"base": base or None, "head": head or None,
                                                              "cats": [c.strip() for c in args.scope.split(",") if c.strip()]})
        print("Preparing CHANGELOG…", end="", flush=True)
        while True:
            st = call(args.server, "/api/changelog/%s" % job["id"])
            if not st["running"]:
                break
            print(".", end="", flush=True)
            time.sleep(1.5)
        print()
        if st.get("error"):
            raise SystemExit("Error: " + st["error"])
        out = args.output or ("CHANGELOG-%s.zip" % job["id"])
        req = urllib.request.Request(job["zip"])
        pw = os.environ.get("VIVER_PASSWORD")
        if pw:
            import base64
            req.add_header("Authorization", "Basic " + base64.b64encode(("cli:" + pw).encode()).decode())
        with urllib.request.urlopen(req, timeout=600) as r, open(out, "wb") as f:
            f.write(r.read())
        res = st["result"]
        print("Saved: %s  (%d VI content, %d visual/layout, %d re-saved only)" % (
            out, res["functional"], res["layout"], res["saved"]))
        return
    if args.status:
        st = call(args.server, "/api/r/%s/status" % name)
        if not st["changes"]:
            print("Clean — nothing to commit.")
        for c in st["changes"]:
            mark = {"added": "+", "deleted": "-", "modified": "~", "renamed": ">"}.get(c["status"], "?")
            print(" %s %s" % (mark, c["path"]))
        return
    if not args.message:
        ap.error("-m/--message is required")
    res = call(args.server, "/api/r/%s/commit" % name, {"message": args.message, "author": args.author})
    s = res["stats"]
    print("Commit %s created: +%d ~%d -%d file(s) (%d VI)" % (res["short"], s["added"], s["modified"], s["deleted"], s["vi_changed"]))
    print(res["url"])


if __name__ == "__main__":
    main()
