#!/usr/bin/env python3
"""Build the release files into OUT (used by .github/workflows/release.yml;
runs locally too):

    python companion/release/make_release_assets.py --version 0.2.0 --out dist

  audiobook.koplugin-<v>.zip              patched KOReader plugin (plugin/build_plugin.py)
  readalong-companion-deploy-<v>.tar.gz   compose files + env examples + README, images
  readalong-companion-deploy-<v>.zip      pinned to <v> and without the build: sections
  SHA256SUMS
"""
import argparse, hashlib, os, re, shutil, subprocess, sys, tarfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANION = os.path.dirname(HERE)
ROOT = os.path.dirname(COMPANION)
DEFAULT_IMAGE = "ghcr.io/ragemarkus/readalong-companion"
BUNDLE_FILES = ["docker-compose.yml", "docker-compose.worker.yml", "env.example", "worker.env.example", "README.md"]


def pin_compose(text, version, image):
    """Drop the '# >>> build' ... '# <<< build' block, pin the default tag and image."""
    text = re.sub(r"\n[ \t]*# >>> build.*?# <<< build[^\n]*", "", text, flags=re.S)
    text = text.replace(DEFAULT_IMAGE, image)
    text = text.replace("${COMPANION_TAG:-latest}", "${COMPANION_TAG:-%s}" % version)
    text = text.replace("latest (default)", "%s (default)" % version)
    text = re.sub(r"(?m)^#   \(or build from this checkout:.*\n", "", text)
    text = re.sub(r"(?m)^#   docker compose up -d --build .*\n", "", text)
    return text


BUNDLE_README = """# readalong-companion {version} deploy bundle

Compose files for the released images ({image}:{version} and :{version}-cuda).
Full documentation: README.md here, or companion/README.md in the repository.

XPS (CPU companion):
    cp env.example .env && chmod 600 .env && mkdir -p data    # edit .env
    docker compose pull && docker compose up -d
    # web UI: http://<xps-host>:8765

G14 (optional GPU worker):
    cp worker.env.example worker.env                           # edit worker.env
    docker compose -f docker-compose.worker.yml pull
    docker compose -f docker-compose.worker.yml up -d

The image tag defaults to {version}; set COMPANION_TAG to change it.
If `docker compose pull` says "denied", the GHCR package is private: make it
public (GitHub > Packages > readalong-companion > Package settings) or run
`docker login ghcr.io` with a token that has read:packages.
"""


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True, help="e.g. 0.2.0 (a leading v is dropped)")
    ap.add_argument("--image", default=DEFAULT_IMAGE)
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--plugin-zip", help="use an already built build/audiobook.koplugin.zip")
    ap.add_argument("--skip-plugin", action="store_true", help="bundle only (tests)")
    a = ap.parse_args()
    version = a.version[1:] if a.version.startswith("v") else a.version
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version):
        sys.exit("bad version %r" % a.version)
    os.makedirs(a.out, exist_ok=True)
    outputs = []

    if not a.skip_plugin:
        built = a.plugin_zip
        if not built:
            subprocess.run([sys.executable, os.path.join(ROOT, "plugin", "build_plugin.py")], check=True)
            built = os.path.join(ROOT, "build", "audiobook.koplugin.zip")
        dest = os.path.join(a.out, "audiobook.koplugin-%s.zip" % version)
        shutil.copyfile(built, dest)
        outputs.append(dest)

    name = "readalong-companion-deploy-%s" % version
    stage = os.path.join(a.out, name)
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    for f in BUNDLE_FILES:
        with open(os.path.join(COMPANION, f), encoding="utf-8") as fh:
            text = fh.read()
        if f.startswith("docker-compose"):
            text = pin_compose(text, version, a.image)
        if f == "env.example":
            text = text.replace("COMPANION_TAG=latest", "COMPANION_TAG=%s" % version)
        with open(os.path.join(stage, f), "w", encoding="utf-8") as fh:
            fh.write(text)
    with open(os.path.join(stage, "BUNDLE.md"), "w", encoding="utf-8") as fh:
        fh.write(BUNDLE_README.format(version=version, image=a.image))

    tgz = os.path.join(a.out, name + ".tar.gz")
    with tarfile.open(tgz, "w:gz") as t:
        t.add(stage, arcname=name)
    zp = os.path.join(a.out, name + ".zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(stage)):
            z.write(os.path.join(stage, f), "%s/%s" % (name, f))
    shutil.rmtree(stage)
    outputs += [tgz, zp]

    with open(os.path.join(a.out, "SHA256SUMS"), "w") as fh:
        for p in outputs:
            fh.write("%s  %s\n" % (sha256(p), os.path.basename(p)))
    for p in outputs:
        print(p)


if __name__ == "__main__":
    main()
