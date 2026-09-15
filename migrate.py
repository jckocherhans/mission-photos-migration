#!/usr/bin/env python3
"""Drive -> Google Photos migration for Jason's mission archive.

Subcommands
  plan                 Scan the Drive folder, dedupe by checksum, write plan.json, print summary
  albums               Create every album in the plan (idempotent; ids cached in state.json)
  upload [--album X] [--limit N]
                       Upload unique files, create media items, add to albums. Resumable.
  verify               Compare album item counts in Google Photos against the plan
  report               Print progress from state.json

State lives in state.json next to this file. Every step is idempotent: re-running skips
anything already done. Temporary downloads go to ./tmp and are deleted after each upload.
"""
import argparse
import collections
import datetime
import hashlib
import json
import os
import sys
import time

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.path.join(HERE, "token.json")
PLAN = os.path.join(HERE, "plan.json")
STATE = os.path.join(HERE, "state.json")
LOG = os.path.join(HERE, "migration.log")
TMP = os.path.join(HERE, "tmp")

ROOT_ID = "1obvJQIvEsjrWbRCLevWFUxuYQz7CCylK"   # copy B (complete, Feb 22 2021 4:56am)
MASTER = "Mission — Mérida, Mexico"
GENERIC = {"photos", "photo", "videos", "video", "originals"}   # folder levels dropped from album names
FOLDER = "application/vnd.google-apps.folder"
DRIVE = "https://www.googleapis.com/drive/v3"
PHOTOS = "https://photoslibrary.googleapis.com/v1"


# ---------- helpers ----------
def log(msg):
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def save_token(c):
    with open(TOKEN, "w") as f:
        f.write(c.to_json())


def creds():
    c = Credentials.from_authorized_user_file(TOKEN)
    if not c.valid:
        c.refresh(Request())
        save_token(c)
    return c


class Api:
    def __init__(self):
        self.c = creds()
        self.s = requests.Session()

    def hdr(self, extra=None):
        if self.c.expired:
            self.c.refresh(Request())
            save_token(self.c)
        h = {"Authorization": "Bearer " + self.c.token}
        if extra:
            h.update(extra)
        return h

    def call(self, method, url, **kw):
        """Request with retry on 429 / 5xx. `data` may be a callable returning a fresh body per attempt."""
        extra = kw.pop("headers", None)
        body = kw.pop("data", None)
        r = None
        for attempt in range(6):
            data = body() if callable(body) else body
            try:
                r = self.s.request(method, url, headers=self.hdr(extra), data=data, timeout=300, **kw)
            finally:
                if callable(body) and hasattr(data, "close"):
                    data.close()
            if r.status_code == 429 or r.status_code >= 500:
                wait = 30 if r.status_code == 429 else 5 * (attempt + 1)
                log(f"  {r.status_code} from {url.split('?')[0]} — waiting {wait}s")
                time.sleep(wait)
                continue
            return r
        r.raise_for_status()
        return r


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


def album_name(path):
    parts = [p for p in path.split("/") if p.lower() not in GENERIC]
    return " · ".join(parts)


# ---------- plan ----------
def walk(api, folder_id, path, out):
    token = None
    while True:
        params = {"q": f"'{folder_id}' in parents and trashed=false", "pageSize": 1000,
                  "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,createdTime,modifiedTime)",
                  "supportsAllDrives": "true"}
        if token:
            params["pageToken"] = token
        r = api.call("GET", f"{DRIVE}/files", params=params)
        r.raise_for_status()
        d = r.json()
        for f in d.get("files", []):
            if f["mimeType"] == FOLDER:
                walk(api, f["id"], f"{path}/{f['name']}" if path else f["name"], out)
            else:
                f["path"] = path
                out.append(f)
        token = d.get("nextPageToken")
        if not token:
            break


def is_media(f):
    return f["mimeType"].startswith(("image/", "video/"))


def cmd_plan(args):
    api = Api()
    files = []
    log("Scanning Drive folder tree…")
    walk(api, ROOT_ID, "", files)
    media = [f for f in files if is_media(f)]
    skipped = [f for f in files if not is_media(f)]
    log(f"{len(files)} files found; {len(media)} photos/videos; {len(skipped)} non-media skipped")
    groups = collections.OrderedDict()
    for f in sorted(media, key=lambda x: (x["path"], x["name"])):
        key = f.get("md5Checksum") or f"{f['name']}|{f.get('size')}"
        g = groups.setdefault(key, {"md5": key, "name": f["name"], "mime": f["mimeType"],
                                    "size": int(f.get("size", 0)), "sources": []})
        g["sources"].append({"id": f["id"], "path": f["path"]})
    items = []
    for g in groups.values():
        albums = sorted({album_name(s["path"]) for s in g["sources"]})
        g["albums"] = albums
        g["drive_id"] = g["sources"][0]["id"]
        g["description"] = "Mission archive · " + " | ".join(s["path"] for s in g["sources"])
        items.append(g)
    album_counts = collections.Counter(a for g in items for a in g["albums"])
    plan = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "root": ROOT_ID,
            "master": MASTER, "albums": sorted(album_counts), "album_counts": album_counts,
            "items": items,
            "skipped": [{"name": f["name"], "path": f["path"], "mime": f["mimeType"],
                         "size": int(f.get("size", 0))} for f in skipped]}
    jsave(PLAN, plan)
    dup_extra = len(media) - len(items)
    log(f"Plan: {len(items)} unique items to upload ({dup_extra} exact duplicates collapsed), "
        f"{len(album_counts)} folder albums + master, {sum(g['size'] for g in items) / 1e9:.1f} GB")
    for a, n in sorted(album_counts.items()):
        print(f"  {n:5d}  {a}")


# ---------- albums ----------
def cmd_albums(args):
    api = Api()
    plan = jload(PLAN, None)
    st = jload(STATE, {"albums": {}, "items": {}})
    if not plan:
        sys.exit("run `plan` first")
    for name in [plan["master"]] + plan["albums"]:
        if name in st["albums"]:
            continue
        r = api.call("POST", f"{PHOTOS}/albums", json={"album": {"title": name}})
        if r.status_code != 200:
            log(f"album create failed {name}: {r.text}")
            sys.exit(1)
        st["albums"][name] = r.json()["id"]
        jsave(STATE, st)
        log(f"album created: {name}")
    log(f"{len(st['albums'])} albums ready")


# ---------- upload ----------
def download(api, file_id, dest):
    with api.s.get(f"{DRIVE}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                   headers=api.hdr(), stream=True, timeout=600) as r:
        r.raise_for_status()
        h = hashlib.md5()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
    return h.hexdigest()


def upload_bytes(api, path, name, mime):
    # The body is a callable so a retried attempt re-reads the file from the start.
    r = api.call("POST", f"{PHOTOS}/uploads", data=lambda: open(path, "rb"), headers={
        "Content-type": "application/octet-stream", "X-Goog-Upload-Content-Type": mime,
        "X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-File-Name": name})
    if r.status_code != 200:
        raise RuntimeError(f"upload failed {r.status_code}: {r.text[:200]}")
    return r.text


def add_to_album(api, st, album, md5s):
    ids = [st["items"][m]["media_id"] for m in md5s]
    r = api.call("POST", f"{PHOTOS}/albums/{st['albums'][album]}:batchAddMediaItems", json={"mediaItemIds": ids})
    if r.status_code != 200:
        log(f"batchAdd failed for {album}: {r.text[:300]}")
        sys.exit(1)
    for m in md5s:
        st["items"][m].setdefault("albums_done", []).append(album)
    jsave(STATE, st)


def flush_batch(api, st, batch):
    """batchCreate up to 50 pending items, then add them to their albums."""
    if not batch:
        return
    body = {"newMediaItems": [{"description": g["description"][:1000],
                               "simpleMediaItem": {"uploadToken": st["items"][g["md5"]]["upload_token"],
                                                   "fileName": g["name"]}} for g in batch]}
    r = api.call("POST", f"{PHOTOS}/mediaItems:batchCreate", json=body)
    if r.status_code != 200:
        log(f"batchCreate failed: {r.text[:300]}")
        sys.exit(1)
    for g, res in zip(batch, r.json()["newMediaItemResults"]):
        code = res.get("status", {}).get("code", 0)
        if code == 0 and "mediaItem" in res:
            st["items"][g["md5"]]["media_id"] = res["mediaItem"]["id"]
            st["items"][g["md5"]].pop("error", None)
        else:
            st["items"][g["md5"]]["error"] = res.get("status", {}).get("message", "?")
            log(f"  create failed for {g['name']}: {st['items'][g['md5']]['error']}")
    jsave(STATE, st)
    by_album = collections.defaultdict(list)
    for g in batch:
        it = st["items"][g["md5"]]
        if "media_id" not in it:
            continue
        for a in g["albums"] + [MASTER]:
            if a not in it.get("albums_done", []):
                by_album[a].append(g["md5"])
    for a, md5s in by_album.items():
        add_to_album(api, st, a, md5s)
    log(f"  batch of {len(batch)} created and filed")
    batch.clear()


def cmd_upload(args):
    api = Api()
    plan = jload(PLAN, None)
    st = jload(STATE, {"albums": {}, "items": {}})
    if not plan or not st["albums"]:
        sys.exit("run `plan` and `albums` first")
    os.makedirs(TMP, exist_ok=True)
    todo = [g for g in plan["items"] if "media_id" not in st["items"].get(g["md5"], {})]
    if args.album:
        todo = [g for g in todo if args.album in g["albums"]]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} items to upload this run")
    batch = []
    for i, g in enumerate(todo, 1):
        it = st["items"].setdefault(g["md5"], {})
        if "upload_token" not in it or time.time() - it.get("upload_time", 0) > 20 * 3600:
            tmp = os.path.join(TMP, g["name"])
            try:
                md5 = download(api, g["drive_id"], tmp)
                if len(g["md5"]) == 32 and md5 != g["md5"]:
                    log(f"  checksum mismatch on {g['name']} — skipping")
                    it["error"] = "checksum"
                    jsave(STATE, st)
                    continue
                it["upload_token"] = upload_bytes(api, tmp, g["name"], g["mime"])
                it["upload_time"] = time.time()
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            jsave(STATE, st)
        batch.append(g)
        log(f"[{i}/{len(todo)}] uploaded {g['name']} ({g['size'] / 1e6:.1f} MB) -> {', '.join(g['albums'])}")
        if len(batch) == 50:
            flush_batch(api, st, batch)
    flush_batch(api, st, batch)
    # pending album adds for items created earlier but not fully filed (e.g. after a crash)
    pending = [g for g in plan["items"] if "media_id" in st["items"].get(g["md5"], {})
               and set(g["albums"] + [MASTER]) - set(st["items"][g["md5"]].get("albums_done", []))]
    if pending:
        log(f"filing {len(pending)} previously created items into remaining albums")
        by_album = collections.defaultdict(list)
        for g in pending:
            for a in set(g["albums"] + [MASTER]) - set(st["items"][g["md5"]].get("albums_done", [])):
                by_album[a].append(g["md5"])
        for a, md5s in by_album.items():
            for k in range(0, len(md5s), 50):
                add_to_album(api, st, a, md5s[k:k + 50])
    cmd_report(args)


# ---------- verify / report ----------
def cmd_verify(args):
    api = Api()
    plan = jload(PLAN, None)
    if not plan:
        sys.exit("run `plan` first")
    expected = collections.Counter(a for g in plan["items"] for a in g["albums"])
    expected[MASTER] = len(plan["items"])
    albums = []
    token = None
    while True:
        params = {"pageSize": 50}
        if token:
            params["pageToken"] = token
        r = api.call("GET", f"{PHOTOS}/albums", params=params)
        r.raise_for_status()
        d = r.json()
        albums += d.get("albums", [])
        token = d.get("nextPageToken")
        if not token:
            break
    ok = True
    for a in sorted(expected):
        got = next((int(x.get("mediaItemsCount", 0)) for x in albums if x["title"] == a), None)
        flag = "OK " if got == expected[a] else "!! "
        if got != expected[a]:
            ok = False
        print(f"{flag}{expected[a]:5d} expected  {str(got):>5} in Photos   {a}")
    print("ALL ALBUMS MATCH" if ok else "MISMATCHES ABOVE")


def cmd_report(args):
    plan = jload(PLAN, None)
    st = jload(STATE, {"albums": {}, "items": {}})
    if not plan:
        print("no plan yet")
        return
    n = len(plan["items"])
    done = sum(1 for g in plan["items"] if "media_id" in st["items"].get(g["md5"], {}))
    err = [g["name"] for g in plan["items"] if "error" in st["items"].get(g["md5"], {})]
    print(f"{done}/{n} items in Google Photos; {len(err)} errors; {len(st['albums'])} albums")
    if err:
        print("errors:", err[:20])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan")
    sub.add_parser("albums")
    sub.add_parser("verify")
    sub.add_parser("report")
    u = sub.add_parser("upload")
    u.add_argument("--album")
    u.add_argument("--limit", type=int)
    a = ap.parse_args()
    {"plan": cmd_plan, "albums": cmd_albums, "upload": cmd_upload, "verify": cmd_verify, "report": cmd_report}[a.cmd](a)
