"""One job: gather the inputs, run the existing generator, validate, publish.

Reuses sidecar/make_readalong_sidecar.py and sidecar/validate_sidecar.py as
subprocesses (so the companion never needs faster-whisper itself) and
publish_sidecar.abs_copy for the atomic copy into the ABS item folder."""
import json
import os
import re
import shutil
import subprocess
import sys
import time

from .config import map_path

AUDIO_ORDER_NOTE = "media.audioFiles order (the same list and order the KOReader plugin downloads)"


class JobError(Exception):
    def __init__(self, msg, retryable=True, review=False):
        super().__init__(msg)
        self.retryable = retryable
        self.review = review


def safe_name(name):
    name = os.path.basename((name or "").replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "_", name).strip(" .")
    return name or "file"


def sidecar_name(ebook_filename):
    """Book.epub -> Book.readalong.json (the plugin matches it to the EPUB by name)."""
    return re.sub(r"\.epub$", "", safe_name(ebook_filename), flags=re.I) + ".readalong.json"


def job_spec(item, path_map=None):
    """Turn an expanded ABS item into the job description shared with workers.
    'local' paths are only filled in on the companion (mounted library)."""
    media = item.get("media") or {}
    audio = media.get("audioFiles") or []
    eb = media.get("ebookFile") or {}
    ebmd = eb.get("metadata") or {}
    ext = (ebmd.get("ext") or os.path.splitext(ebmd.get("filename") or "")[1]).lower()
    if not audio:
        raise JobError("ABS item has no audio files", retryable=False)
    if not eb or ext != ".epub":
        raise JobError("ABS item has no EPUB attached", retryable=False)
    pm = path_map or []
    return {
        "item_id": item["id"],
        "title": (media.get("metadata") or {}).get("title") or item["id"],
        "server_path": item.get("path"),
        "folder_local": map_path(pm, item.get("path")),
        "audio": [{"ino": af["ino"], "filename": (af.get("metadata") or {}).get("filename") or "track%d" % i,
                   "size": (af.get("metadata") or {}).get("size"),
                   "local": map_path(pm, (af.get("metadata") or {}).get("path"))}
                  for i, af in enumerate(audio, 1)],
        "ebook": {"ino": eb["ino"], "filename": ebmd.get("filename") or "book.epub", "size": ebmd.get("size"),
                  "local": map_path(pm, ebmd.get("path"))},
        "sidecars": [lf["metadata"]["filename"] for lf in item.get("libraryFiles") or []
                     if (lf.get("metadata") or {}).get("filename", "").lower().endswith(".readalong.json")],
    }


def public_spec(spec):
    """What a remote worker gets: no server paths."""
    strip = lambda f: {k: v for k, v in f.items() if k != "local"}
    return {"item_id": spec["item_id"], "title": spec["title"],
            "audio": [strip(a) for a in spec["audio"]], "ebook": strip(spec["ebook"])}


def _link_or_fetch(entry, dest, fetch):
    src = entry.get("local")
    if os.path.lexists(dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        os.remove(dest)
    if src and os.path.isfile(src):
        try:
            os.symlink(os.path.abspath(src), dest)
        except OSError:
            shutil.copyfile(src, dest)
        return dest
    fetch(entry["ino"], dest)
    return dest


def materialise(spec, workdir, fetch):
    """workdir/audio/001-<file>... and workdir/<Book>.epub, in ABS order.
    Mounted files are symlinked; anything else is fetched (ABS API or the
    companion's file proxy) with fetch(ino, dest)."""
    adir = os.path.join(workdir, "audio")
    os.makedirs(adir, exist_ok=True)
    audio = []
    for i, a in enumerate(spec["audio"], 1):
        audio.append(_link_or_fetch(a, os.path.join(adir, "%03d-%s" % (i, safe_name(a["filename"]))), fetch))
    epub = _link_or_fetch(spec["ebook"], os.path.join(workdir, safe_name(spec["ebook"]["filename"])), fetch)
    return epub, audio


def _tail(path, n=3000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - n, 0))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def generate(cfg, spec, epub, audio, workdir, heartbeat=None, interval=30):
    """Run make_readalong_sidecar.py.  heartbeat() is called every `interval`
    seconds; if it returns False the job was taken away and the run stops."""
    out = os.path.join(workdir, sidecar_name(spec["ebook"]["filename"]))
    cmd = [cfg.python, cfg.generator, epub, *audio, "--abs-item-id", spec["item_id"],
           "--model", cfg.model, "--device", cfg.device, "--language", cfg.language,
           "--transcript-cache", os.path.join(workdir, "transcripts"), "-o", out]
    log_path = os.path.join(workdir, "generator.log")
    t0 = time.time()
    with open(log_path, "ab") as log:
        p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=cfg.sidecar_dir)
        while True:
            try:
                rc = p.wait(timeout=interval)
                break
            except subprocess.TimeoutExpired:
                if time.time() - t0 > cfg.generator_timeout:
                    p.kill(); p.wait()
                    raise JobError("generator timed out after %ds" % cfg.generator_timeout)
                if heartbeat and heartbeat() is False:
                    p.kill(); p.wait()
                    raise JobError("lease lost; stopped", retryable=True)
    if rc != 0 or not os.path.exists(out):
        raise JobError("generator exited %s: %s" % (rc, _tail(log_path)[-1500:]))
    return out


def _json_block(text):
    m = re.search(r"^\{.*?^\}", text, re.S | re.M)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}


def validate(cfg, sidecar_path, item_id, audio_dir):
    """Structural + quality checks.  Raises JobError(review=True) when the
    sidecar must not be published.  Returns a summary dict."""
    try:
        with open(sidecar_path, encoding="utf-8") as f:
            sc = json.load(f)
    except (OSError, ValueError) as e:
        raise JobError("sidecar unreadable: %s" % e, retryable=False, review=True)
    if sc.get("format") != "koreader-readalong" or sc.get("version") != 1 or not sc.get("cues"):
        raise JobError("not a koreader-readalong v1 sidecar with cues", retryable=False, review=True)
    got = (sc.get("audio") or {}).get("abs_item_id")
    if got != item_id:
        raise JobError("sidecar is for item %r, expected %r" % (got, item_id), retryable=False, review=True)
    stats = sc.get("stats") or {}
    mis = stats.get("matched_in_span")
    if mis is not None and mis < cfg.min_match:
        raise JobError("only %.1f%% of narrated words matched the EPUB (MIN_MATCH %.0f%%): wrong edition or "
                       "abridged audio?" % (mis * 100, cfg.min_match * 100), retryable=False, review=True)
    # sidecar/validate_sidecar.py: schema/timing checks and the xpointer check
    # against the EPUB next to the sidecar (no audio spot-check: --samples 0).
    r = subprocess.run([cfg.python, cfg.validator, sidecar_path, audio_dir, "--samples", "0"],
                       capture_output=True, text=True, cwd=cfg.sidecar_dir, timeout=1800)
    out = r.stdout or ""
    summary = _json_block(out)
    m = re.search(r"XPOINTERS: (\d+)/(\d+)", out)
    xp = [int(m.group(1)), int(m.group(2))] if m else None
    if r.returncode != 0:
        raise JobError("validate_sidecar.py failed (rc %d): %s %s" % (r.returncode, json.dumps(summary)[:500],
                       (r.stderr or "")[-800:]), retryable=False, review=True)
    if xp is None:
        raise JobError("validate_sidecar.py did not check xpointers (EPUB not next to the sidecar?)",
                       retryable=False, review=True)
    if xp[1] and xp[0] / xp[1] < cfg.min_xpointer:
        raise JobError("only %d/%d sentence xpointers land on the right word" % tuple(xp),
                       retryable=False, review=True)
    return {"stats": stats, "validator": summary, "xpointers": xp, "book_file": (sc.get("book") or {}).get("file")}


def publish(cfg, spec, sidecar_path, abs_client=None, log=lambda m: None):
    """Keep a copy (served by the API) and, when the item folder is mounted,
    copy it in atomically exactly like publish_sidecar.py --abs-folder.
    Returns (status, published_path)."""
    os.makedirs(cfg.sidecars_dir, exist_ok=True)
    keep = os.path.join(cfg.sidecars_dir, "%s.readalong.json" % spec["item_id"])
    shutil.copyfile(sidecar_path, keep + ".part")
    os.replace(keep + ".part", keep)
    folder = spec.get("folder_local")
    if not folder or not os.path.isdir(folder):
        log("item folder not mounted here (%s); kept the sidecar in the companion only"
            % ("no ABS_PATH_MAP match for " + str(spec.get("server_path")) if not folder else folder))
        return "generated", None
    if cfg.sidecar_dir not in sys.path:
        sys.path.insert(0, cfg.sidecar_dir)
    import publish_sidecar
    dest = publish_sidecar.abs_copy(sidecar_path, folder)
    if cfg.abs_scan_after_publish and abs_client is not None:
        try:
            abs_client.scan_item(spec["item_id"])
            log("asked ABS to rescan the item")
        except Exception as e:  # not fatal: ABS's watcher / next scan will list it
            log("ABS item scan failed (needs an admin token): %s" % e)
    return "done", dest
