#!/usr/bin/env python3
"""Post-migration Drive cleanup.

  python3 cleanup.py plan      Walk the archive folders, classify every file, write cleanup_plan.json, print counts
  python3 cleanup.py run       Move leftovers into the destination folder, trash verified files, trash the roots

Rules
  * A file is DELETED (moved to Drive trash, recoverable for 30 days) only if its checksum matches an item
    that Google Photos confirmed it holds (state.json: media_id present, not unprocessable / preexisting).
  * Everything else is MOVED into <destination>/Archive leftovers/<original path>, one copy per checksum.
    Extra identical copies of a leftover are trashed.
  * After every file is handled, the three archive root folders are trashed (they then hold only trashed
    files and empty sub-folders). A final walk confirms nothing live remains before that happens.
Progress is recorded in cleanup_state.json so the run can be resumed.
"""
import collections
import concurrent.futures
import datetime
import json
import os
import sys
import threading
import time

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.path.join(HERE, "token.json")
STATE = os.path.join(HERE, "state.json")
CPLAN = os.path.join(HERE, "cleanup_plan.json")
CSTATE = os.path.join(HERE, "cleanup_state.json")
LOG = os.path.join(HERE, "cleanup.log")
DRIVE = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"

ROOTS = {  # label -> folder id
    "main archive (Pictures, used for the migration)": "1obvJQIvEsjrWbRCLevWFUxuYQz7CCylK",
    "mirror copy (Jasons Folder Don_t Touch)": "1nrx6ve1EQbouB33k3IPA_jdlKyrL-tX8",
    "2015 copy (Pictures, empty)": "0B2VhYkpB3c3hflBpUDFxV21YNndKY3l3X1hhdllWTldWUFJUcEVka1VkekdrYl95aG16MTA",
}
DEST = "1UY_mnZAouKe6MtRU5fyNlOaGL8fdq5ct"          # "Jason Merida Mexico Mission"
LEFTOVERS_NAME = "Archive leftovers"

_lock = threading.RLock()   # re-entrant: log() is called while the lock is held


def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}"
    with _lock:
        print(line, flush=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")


def jload(p, default):
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return json.load(f)


def jsave(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, p)


class Api:
    def __init__(self):
        self.c = Credentials.from_authorized_user_file(TOKEN)
        if not self.c.valid:
            self.c.refresh(Request())
        self.local = threading.local()

    def hdr(self):
        with _lock:
            if self.c.expired:
                self.c.refresh(Request())
        return {"Authorization": "Bearer " + self.c.token}

    def session(self):
        if not hasattr(self.local, "s"):
            self.local.s = requests.Session()
        return self.local.s

    def call(self, method, url, **kw):
        for attempt in range(8):
            try:
                r = self.session().request(method, url, headers=self.hdr(), timeout=120, **kw)
            except requests.exceptions.RequestException as e:
                if attempt == 7:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (403, 429) and ("rate" in r.text.lower() or "quota" in r.text.lower()) \
                    or r.status_code >= 500:
                time.sleep(min(60, 3 * 2 ** attempt))
                continue
            return r
        r.raise_for_status()
        return r


def walk(api, folder_id, path, out, folders):
    token = None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 1000,
                  "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum)", "supportsAllDrives": "true"}
        if token:
            params["pageToken"] = token
        r = api.call("GET", f"{DRIVE}/files", params=params)
        r.raise_for_status()
        d = r.json()
        for f in d.get("files", []):
            if f["mimeType"] == FOLDER:
                folders.append({"id": f["id"], "path": f"{path}/{f['name']}"})
                walk(api, f["id"], f"{path}/{f['name']}", out, folders)
            else:
                f["path"] = path
                out.append(f)
        token = d.get("nextPageToken")
        if not token:
            break


def cmd_plan(args):
    api = Api()
    st = jload(STATE, {"items": {}})
    safe = {md5 for md5, it in st["items"].items()
            if "media_id" in it and not it.get("unprocessable") and not it.get("preexisting") and len(md5) == 32}
    log(f"{len(safe)} checksums confirmed in Google Photos")
    files, folders = [], []
    for label, fid in ROOTS.items():
        before = len(files)
        walk(api, fid, label, files, folders)
        log(f"{label}: {len(files) - before} files")
    delete, keep, dup = [], [], []
    seen = {}
    for f in sorted(files, key=lambda x: (list(ROOTS).index(x["path"].split("/")[0]), x["path"], x["name"])):
        md5 = f.get("md5Checksum")
        if md5 in safe:
            delete.append(f)
        elif md5 and md5 in seen:
            f["duplicate_of"] = seen[md5]
            dup.append(f)
        else:
            if md5:
                seen[md5] = f["id"]
            keep.append(f)
    plan = {"generated": datetime.datetime.now().isoformat(timespec="seconds"),
            "delete": delete, "keep": keep, "duplicate_leftovers": dup, "folders": folders}
    jsave(CPLAN, plan)
    gb = lambda xs: sum(int(x.get("size", 0)) for x in xs) / 1e9
    log(f"DELETE (verified in Photos): {len(delete)} files, {gb(delete):.1f} GB")
    log(f"KEEP -> leftovers: {len(keep)} files, {gb(keep):.2f} GB")
    log(f"extra identical copies of leftovers (trashed): {len(dup)} files, {gb(dup):.2f} GB")
    log(f"folders to trash afterwards: {len(folders)} + {len(ROOTS)} roots")
    print("\nKEEP by type:")
    for k, n in collections.Counter(x["mimeType"] for x in keep).most_common():
        print(f"  {n:4d}  {k}")
    print("\nKEEP files that are photos/videos (not verified in Photos):")
    for x in keep:
        if x["mimeType"].startswith(("image/", "video/")):
            print(f"  {x['path']} / {x['name']}  {int(x.get('size', 0)) / 1e6:.1f} MB")


def ensure_folder(api, cache, parent, name):
    key = (parent, name)
    if key in cache:
        return cache[key]
    q = f"'{parent}' in parents and name = '{name.replace(chr(39), chr(92) + chr(39))}' and mimeType = '{FOLDER}' and trashed=false"
    r = api.call("GET", f"{DRIVE}/files", params={"q": q, "fields": "files(id)"})
    r.raise_for_status()
    hits = r.json().get("files", [])
    if hits:
        fid = hits[0]["id"]
    else:
        r = api.call("POST", f"{DRIVE}/files", params={"fields": "id"},
                     json={"name": name, "mimeType": FOLDER, "parents": [parent]})
        r.raise_for_status()
        fid = r.json()["id"]
        log(f"created folder {name}")
    cache[key] = fid
    return fid


def cmd_run(args):
    api = Api()
    plan = jload(CPLAN, None)
    if not plan:
        sys.exit("run `plan` first")
    cs = jload(CSTATE, {"done": {}})
    cache = {}

    # 1. move leftovers, preserving their original path under the destination
    left_root = ensure_folder(api, cache, DEST, LEFTOVERS_NAME)
    todo = [f for f in plan["keep"] if f["id"] not in cs["done"]]
    log(f"moving {len(todo)} leftover files (of {len(plan['keep'])})")
    for f in todo:
        parent = left_root
        for part in f["path"].split("/"):
            parent = ensure_folder(api, cache, parent, part)
        r = api.call("GET", f"{DRIVE}/files/{f['id']}", params={"fields": "parents"})
        r.raise_for_status()
        old = ",".join(r.json().get("parents", []))
        r = api.call("PATCH", f"{DRIVE}/files/{f['id']}",
                     params={"addParents": parent, "removeParents": old, "fields": "id"})
        if r.status_code != 200:
            log(f"MOVE FAILED {f['path']}/{f['name']}: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        cs["done"][f["id"]] = "moved"
        jsave(CSTATE, cs)
        log(f"moved {f['path']}/{f['name']}")

    # 2. trash verified files and duplicate leftovers, in parallel
    to_trash = [f for f in plan["delete"] + plan["duplicate_leftovers"] if f["id"] not in cs["done"]]
    log(f"trashing {len(to_trash)} files (of {len(plan['delete']) + len(plan['duplicate_leftovers'])})")
    count = [0]

    def trash(f):
        r = api.call("PATCH", f"{DRIVE}/files/{f['id']}", params={"fields": "id"}, json={"trashed": True})
        if r.status_code != 200:
            raise RuntimeError(f"TRASH FAILED {f['path']}/{f['name']}: {r.status_code} {r.text[:200]}")
        with _lock:
            cs["done"][f["id"]] = "trashed"
            count[0] += 1
            if count[0] % 50 == 0:
                jsave(CSTATE, cs)
                log(f"  trashed {count[0]}/{len(to_trash)}")
        return f["id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for _ in ex.map(trash, to_trash):
            pass
    jsave(CSTATE, cs)
    log(f"trashed {count[0]} files this run")

    # 3. confirm nothing live remains, then trash the root folders
    files, folders = [], []
    for label, fid in ROOTS.items():
        walk(api, fid, label, files, folders)
    if files:
        log(f"STOP: {len(files)} live files still inside the archive folders, roots NOT trashed:")
        for f in files[:20]:
            log(f"   {f['path']}/{f['name']}")
        sys.exit(1)
    for label, fid in ROOTS.items():
        if fid in cs["done"]:
            continue
        r = api.call("PATCH", f"{DRIVE}/files/{fid}", params={"fields": "id"}, json={"trashed": True})
        if r.status_code != 200:
            log(f"ROOT TRASH FAILED {label}: {r.status_code} {r.text[:200]}")
            sys.exit(1)
        cs["done"][fid] = "root trashed"
        jsave(CSTATE, cs)
        log(f"trashed root folder: {label}")
    moved = sum(1 for v in cs["done"].values() if v == "moved")
    trashed = sum(1 for v in cs["done"].values() if v == "trashed")
    log(f"DONE: {moved} files moved to leftovers, {trashed} files trashed, {len(ROOTS)} root folders trashed")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "run"])
    a = ap.parse_args()
    {"plan": cmd_plan, "run": cmd_run}[a.cmd](a)
