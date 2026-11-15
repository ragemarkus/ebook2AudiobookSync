#!/usr/bin/env python3
"""Store a read-along sidecar with the book on the server.

Audiobookshelf (recommended): put BOOK.readalong.json in the audiobook's own
folder.  ABS lists it as a library file of that item ("metadata" file type,
not parsed) and the KOReader plugin fetches it when it downloads the item.

    # 1) copy into the item folder (a mounted share / path on the ABS host)
    python publish_sidecar.py BOOK.readalong.json --abs-folder "/mnt/audiobooks/Author/Title"
    # 2) ask ABS to rescan that item now (else the folder watcher / next scan picks it up)
    ABS_URL=... ABS_TOKEN=<admin token> python publish_sidecar.py BOOK.readalong.json --abs-scan ITEM_ID

Grimmory / Booklore (optional): attach it to the book as a supplementary file.

    GRIMMORY_URL=https://books.example GRIMMORY_USER=... GRIMMORY_PASSWORD=... \\
        python publish_sidecar.py BOOK.readalong.json --grimmory-book-id 123
    # (or GRIMMORY_TOKEN=<JWT access token> instead of user/password)

Optional Cloudflare Access service-token headers for either server:
CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET.  Secrets come from the
environment only and are never printed.
"""
import argparse, json, os, shutil, sys, urllib.request, uuid


def _headers(extra=None):
    h = {"User-Agent": "koreader-readalong-publish"}
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        h["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        h["CF-Access-Client-Secret"] = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    h.update(extra or {})
    return h


def _call(url, method="GET", data=None, headers=None):
    r = urllib.request.Request(url, data=data, method=method, headers=_headers(headers))
    with urllib.request.urlopen(r, timeout=120) as resp:
        body = resp.read()
        return resp.status, body


def check_sidecar(path):
    with open(path, encoding="utf-8") as f:
        sc = json.load(f)
    if sc.get("format") != "koreader-readalong" or not sc.get("cues"):
        sys.exit("%s is not a read-along sidecar" % path)
    return sc


def abs_copy(path, folder):
    if not os.path.isdir(folder):
        sys.exit("ABS item folder not found: %s" % folder)
    dest = os.path.join(folder, os.path.basename(path))
    if not dest.endswith(".readalong.json"):
        dest = os.path.join(folder, os.path.splitext(os.path.basename(path))[0] + ".readalong.json")
    tmp = dest + ".part"
    shutil.copyfile(path, tmp)
    os.replace(tmp, dest)
    print("copied ->", dest, file=sys.stderr)
    return dest


def abs_scan(item_id):
    url = os.environ["ABS_URL"].rstrip("/") + "/api/items/%s/scan" % item_id
    st, body = _call(url, "POST", b"", {"Authorization": "Bearer " + os.environ["ABS_TOKEN"]})
    res = {}
    try:
        res = json.loads(body or b"{}")
    except ValueError:
        pass
    print("ABS scan:", st, res.get("result", ""), file=sys.stderr)
    return res


def abs_has_sidecar(item_id):
    url = os.environ["ABS_URL"].rstrip("/") + "/api/items/%s?expanded=1" % item_id
    _, body = _call(url, headers={"Authorization": "Bearer " + os.environ["ABS_TOKEN"]})
    files = json.loads(body).get("libraryFiles") or []
    return [f["metadata"]["filename"] for f in files
            if f.get("metadata", {}).get("filename", "").lower().endswith(".readalong.json")]


def grimmory_token(base):
    if os.environ.get("GRIMMORY_TOKEN"):
        return os.environ["GRIMMORY_TOKEN"]
    data = json.dumps({"username": os.environ["GRIMMORY_USER"],
                       "password": os.environ["GRIMMORY_PASSWORD"]}).encode()
    _, body = _call(base + "/api/v1/auth/login", "POST", data, {"Content-Type": "application/json"})
    return json.loads(body)["accessToken"]


def multipart(field, filename, content, ctype="application/json"):
    b = uuid.uuid4().hex
    parts = [("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
              "Content-Type: %s\r\n\r\n" % (b, field, filename.replace('"', "'"), ctype)).encode(),
             content, ("\r\n--%s--\r\n" % b).encode()]
    return b"".join(parts), "multipart/form-data; boundary=" + b


def grimmory_upload(path, book_id):
    base = os.environ["GRIMMORY_URL"].rstrip("/")
    auth = {"Authorization": "Bearer " + grimmory_token(base)}
    name = os.path.basename(path)
    _, body = _call(base + "/api/v1/books/%s/files?isBook=false" % book_id, headers=auth)
    for f in json.loads(body or b"[]"):
        if (f.get("fileName") or f.get("filename") or "") == name:
            print("Grimmory: %s already attached (file id %s); delete it first to replace" % (name, f.get("id")),
                  file=sys.stderr)
            return f
    with open(path, "rb") as fh:
        data, ctype = multipart("file", name, fh.read())
    q = "isBook=false&description=" + urllib.request.quote("KOReader read-along timings")
    _, body = _call(base + "/api/v1/books/%s/files?%s" % (book_id, q), "POST", data,
                    dict(auth, **{"Content-Type": ctype}))
    res = json.loads(body or b"{}")
    print("Grimmory: attached as supplementary file id", res.get("id"), file=sys.stderr)
    return res


def publish(path, abs_folder=None, abs_item_id=None, scan=False, grimmory_book_id=None):
    sc = check_sidecar(path)
    if abs_folder:
        abs_copy(path, abs_folder)
    item_id = abs_item_id or sc.get("audio", {}).get("abs_item_id")
    if scan:
        if not item_id:
            sys.exit("--abs-scan needs the ABS item id (--abs-item-id or audio.abs_item_id in the sidecar)")
        abs_scan(item_id)
        found = abs_has_sidecar(item_id)
        print("ABS item now lists:", ", ".join(found) or "(no .readalong.json yet - check the folder)",
              file=sys.stderr)
    if grimmory_book_id:
        grimmory_upload(path, grimmory_book_id)


def add_args(ap):
    g = ap.add_argument_group("store the sidecar with the book")
    g.add_argument("--abs-folder", help="copy the sidecar into this Audiobookshelf item folder")
    g.add_argument("--abs-scan", action="store_true",
                   help="POST /api/items/ID/scan so ABS lists it now (ABS_URL, ABS_TOKEN of an admin)")
    g.add_argument("--grimmory-book-id", help="attach to this Grimmory/Booklore book as a supplementary file "
                   "(GRIMMORY_URL + GRIMMORY_TOKEN or GRIMMORY_USER/GRIMMORY_PASSWORD)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sidecar")
    ap.add_argument("--abs-item-id", help="defaults to audio.abs_item_id in the sidecar")
    add_args(ap)
    a = ap.parse_args()
    publish(a.sidecar, a.abs_folder, a.abs_item_id, a.abs_scan, a.grimmory_book_id)


if __name__ == "__main__":
    main()
