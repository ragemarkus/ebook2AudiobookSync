#!/usr/bin/env python3
"""Build the patched audiobook.koplugin (Cloudflare Access + read-along sidecar).

Downloads the upstream release zip (stradichenko/audiobook.koplugin v0.6.13,
~180 MB, verified by sha256), unpacks it and copies overlay/ over it.

    python plugin/build_plugin.py                 # -> build/audiobook.koplugin + build/audiobook.koplugin.zip
    python plugin/build_plugin.py --test          # also run the read-along Lua tests (pip install lupa)
    python plugin/build_plugin.py --zip PATH      # use an already downloaded release zip

Works on Windows, macOS and Linux (Python 3.8+ only).  The same changes as
git patches against tag v0.6.13 (commit 9a59606) are in plugin/patches/.
"""
import argparse, glob, hashlib, os, shutil, subprocess, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UPSTREAM_TAG = "v0.6.13"
UPSTREAM_COMMIT = "9a59606baddc67d31ed801fe20c942c425aed396"
RELEASE_URL = ("https://github.com/stradichenko/audiobook.koplugin/releases/download/"
               "v0.6.13/audiobook-koplugin-v0.6.13.zip")
RELEASE_SHA256 = "d0f559a5d4421eb38b3b0b33fd69c8fd7d29111ed89f3d10a9c990ec272bff4d"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "build"))
    ap.add_argument("--zip", help="upstream release zip already on disk")
    ap.add_argument("--test", action="store_true", help="run plugin/tests with lupa (LuaJIT)")
    ap.add_argument("--no-zip", action="store_true", help="don't create the output zip")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    rel = a.zip or os.path.join(a.out, os.path.basename(RELEASE_URL))
    if not os.path.exists(rel):
        print("downloading", RELEASE_URL)
        urllib.request.urlretrieve(RELEASE_URL, rel + ".part")
        os.replace(rel + ".part", rel)
    got = sha256(rel)
    if got != RELEASE_SHA256:
        sys.exit("unexpected sha256 for %s: %s" % (rel, got))

    plug = os.path.join(a.out, "audiobook.koplugin")
    shutil.rmtree(plug, ignore_errors=True)
    with zipfile.ZipFile(rel) as z:
        z.extractall(a.out)
    overlay = os.path.join(HERE, "overlay")
    n = 0
    for src in glob.glob(os.path.join(overlay, "**", "*"), recursive=True):
        if os.path.isfile(src):
            dst = os.path.join(plug, os.path.relpath(src, overlay))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    print("overlaid %d files onto upstream %s" % (n, UPSTREAM_TAG))

    if a.test:
        dev = os.path.join(plug, "dev")
        os.makedirs(dev, exist_ok=True)
        for t in glob.glob(os.path.join(HERE, "tests", "*.lua")):
            shutil.copy2(t, dev)
        runner = os.path.join(ROOT, "tools", "luajit.py")
        # (dev/test_readalong_sidecar.lua DEMO_DIR additionally loads a real
        #  book: DEMO_DIR with "The Gift of the Magi.epub", its sidecar and magi.mp3)
        for t, extra in (("test_readalong_sidecar.lua", []), ("test_readalong_sidecar_highlight.lua", [])):
            r = subprocess.run([sys.executable, runner, os.path.join("dev", t)] + extra, cwd=plug)
            if r.returncode:
                sys.exit("test failed: " + t)
        for t in glob.glob(os.path.join(HERE, "tests", "*.lua")):
            os.remove(os.path.join(dev, os.path.basename(t)))

    if not a.no_zip:
        base = os.path.join(a.out, "audiobook.koplugin")
        shutil.make_archive(base, "zip", a.out, "audiobook.koplugin")
        print("wrote", base + ".zip")
    print("copy %s into KOReader's plugins folder" % plug)


if __name__ == "__main__":
    main()
