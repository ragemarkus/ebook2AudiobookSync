#!/usr/bin/env python3
"""Download an Audiobookshelf item's audio tracks (in ABS order) and its
ebook, ready for make_readalong_sidecar.py.

    export ABS_URL=http://<abs-host>:13378        # LAN address, or the public one
    export ABS_TOKEN=...                           # ABS user API token (Settings > Users > you)
    # only when going through Cloudflare Access:
    export CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...
    python abs_fetch.py "Gift of the Magi" -o work/

Prints the item id and writes work/<title>/{audio/, book.epub, abs_item.json}.
Secrets are read from the environment only and never printed.
"""
import argparse, json, os, re, sys, urllib.parse, urllib.request


def req(path, raw=False):
    url = os.environ["ABS_URL"].rstrip("/") + path
    h = {"Authorization": "Bearer " + os.environ["ABS_TOKEN"]}
    if os.environ.get("CF_ACCESS_CLIENT_ID"):
        h["CF-Access-Client-Id"] = os.environ["CF_ACCESS_CLIENT_ID"]
        h["CF-Access-Client-Secret"] = os.environ.get("CF_ACCESS_CLIENT_SECRET", "")
    r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120)
    return r if raw else json.load(r)


def download(path, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    with req(path, raw=True) as r, open(dest + ".part", "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(dest + ".part", dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="title to search for, or an item id with --id")
    ap.add_argument("--id", action="store_true", help="query is an ABS library item id")
    ap.add_argument("-o", "--out", default="work")
    a = ap.parse_args()
    if a.id:
        item_id = a.query
    else:
        hits = []
        for lib in req("/api/libraries")["libraries"]:
            if lib.get("mediaType") != "book":
                continue
            res = req("/api/libraries/%s/search?q=%s&limit=10" % (lib["id"], urllib.parse.quote(a.query)))
            hits += [b["libraryItem"] for b in res.get("book", [])]
        if not hits:
            sys.exit("no match for %r" % a.query)
        for i, h in enumerate(hits):
            md = h["media"]["metadata"]
            print("%d) %s - %s  [%s]" % (i + 1, md.get("title"), md.get("authorName"), h["id"]), file=sys.stderr)
        item_id = hits[0]["id"]
    item = req("/api/items/%s?expanded=1" % item_id)
    media = item["media"]
    title = media["metadata"].get("title") or item_id
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()
    d = os.path.join(a.out, safe)
    os.makedirs(os.path.join(d, "audio"), exist_ok=True)
    # Same list and order the KOReader plugin downloads (media.audioFiles),
    # so global offsets in the sidecar match its ABS cache.
    files = media.get("audioFiles") or []
    names = []
    for i, af in enumerate(files, 1):
        name = "%03d-%s" % (i, af["metadata"]["filename"])
        print("audio %d/%d %s" % (i, len(files), af["metadata"]["filename"]), file=sys.stderr)
        download("/api/items/%s/file/%s/download" % (item_id, af["ino"]), os.path.join(d, "audio", name))
        names.append(name)
    ebook = media.get("ebookFile")
    if ebook and ebook["metadata"]["ext"].lower() == ".epub":
        download("/api/items/%s/file/%s/download" % (item_id, ebook["ino"]), os.path.join(d, safe + ".epub"))
        print("ebook: %s" % ebook["metadata"]["filename"], file=sys.stderr)
    else:
        print("no EPUB attached to this ABS item: put the EPUB in %s yourself" % d, file=sys.stderr)
    json.dump({"id": item_id, "title": title, "audio": names,
               # folder of the item on the ABS server: --abs-folder target
               # (translate to wherever that share is mounted on this PC)
               "server_path": item.get("path"),
               "readalong": [f["metadata"]["filename"] for f in item.get("libraryFiles") or []
                             if f["metadata"]["filename"].lower().endswith(".readalong.json")],
               "excluded": [f["metadata"]["filename"] for f in files if f.get("exclude")]},
              open(os.path.join(d, "abs_item.json"), "w"), indent=1)
    print(item_id)


if __name__ == "__main__":
    main()
