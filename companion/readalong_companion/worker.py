"""Remote worker (e.g. the G14's GPU): pulls jobs from the companion, gets the
audio + EPUB through the companion's file proxy (so it needs no ABS or
Cloudflare credentials), runs the generator, checks the result and uploads
the sidecar.  The companion validates it again and writes it into ABS."""
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request

from . import pipeline

log = logging.getLogger("readalong.worker")


class CompanionError(Exception):
    pass


class Client:
    def __init__(self, url, token, name, timeout=60):
        self.url = url.rstrip("/")
        self._token = token
        self.name = name
        self.timeout = timeout

    def _req(self, method, path, data=None, ctype="application/json", timeout=None):
        h = {"Authorization": "Bearer " + self._token, "X-Worker-Name": self.name,
             "User-Agent": "readalong-worker"}
        if data is not None:
            h["Content-Type"] = ctype
        r = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(r, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            msg = e.read()[:500].decode("utf-8", "replace")
            raise CompanionError("%s %s -> HTTP %s %s" % (method, path, e.code, msg)) from None
        except urllib.error.URLError as e:
            raise CompanionError("%s %s -> %s" % (method, path, e.reason)) from None

    def _json(self, method, path, obj=None, raw=None, timeout=None):
        data = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        with self._req(method, path, data, timeout=timeout) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)

    def claim(self):
        st, d = self._json("POST", "/api/worker/claim", {})
        return d["job"] if st == 200 and d else None

    def heartbeat(self, item_id):
        try:
            return bool(self._json("POST", "/api/worker/jobs/%s/heartbeat" % item_id, {})[1].get("ok"))
        except CompanionError as e:
            if "HTTP 409" in str(e):
                return False            # lease lost: stop
            log.warning("heartbeat: %s", e)
            return True                 # transient: keep going

    def download(self, item_id, ino, dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        with self._req("GET", "/api/worker/jobs/%s/file/%s" % (item_id, ino), timeout=600) as r, \
                open(dest + ".part", "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        os.replace(dest + ".part", dest)
        return dest

    def complete(self, item_id, path):
        with open(path, "rb") as f:
            return self._json("POST", "/api/worker/jobs/%s/complete" % item_id, raw=f.read(), timeout=1800)[1]

    def fail(self, item_id, error, retryable=True, review=False):
        return self._json("POST", "/api/worker/jobs/%s/fail" % item_id,
                          {"error": error[-4000:], "retryable": retryable, "review": review})[1]


class _Beat:
    """Heartbeats on a timer while files download (the generator loop beats itself)."""
    def __init__(self, fn, every):
        self.fn, self.every, self.stop = fn, every, threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(self.every):
            self.fn()

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.stop.set()


def run_job(cfg, client, job, model_override=None):
    item_id = job["item_id"]
    wd = os.path.join(cfg.work_dir, item_id)
    os.makedirs(wd, exist_ok=True)
    beat_every = max(10, min(60, job.get("lease_seconds", 900) // 4))
    hb = lambda: client.heartbeat(item_id)
    try:
        if model_override:
            cfg.model = model_override
        elif job.get("model"):
            cfg.model = job["model"]
        with _Beat(hb, beat_every):
            epub, audio = pipeline.materialise(job, wd, lambda ino, dest: client.download(item_id, ino, dest))
        out = pipeline.generate(cfg, job, epub, audio, wd, heartbeat=hb, interval=beat_every)
        pipeline.validate(cfg, out, item_id, os.path.join(wd, "audio"))
        res = client.complete(item_id, out)
        log.info("%s: uploaded, companion says %s", item_id, res)
        if res and res.get("status") in ("done", "generated") and not cfg.keep_work:
            shutil.rmtree(wd, ignore_errors=True)
        return res
    except pipeline.JobError as e:
        log.warning("%s: %s", item_id, e)
        if "lease lost" in str(e):
            return {"status": "lease_lost"}
        return client.fail(item_id, str(e), e.retryable, e.review)
    except CompanionError as e:
        log.warning("%s: %s", item_id, e)
        try:
            return client.fail(item_id, str(e), True)
        except CompanionError:
            return {"status": "error", "error": str(e)}
    except Exception as e:
        log.exception("%s", item_id)
        try:
            return client.fail(item_id, "%s: %s" % (type(e).__name__, e), True)
        except CompanionError:
            return {"status": "error", "error": str(e)}


def run_once(cfg, client=None):
    client = client or Client(cfg.companion_url, cfg.worker_token, cfg.worker_name)
    job = client.claim()
    if not job:
        return None
    log.info("job %s: %s (%d tracks)", job["item_id"], job.get("title"), len(job.get("audio", [])))
    return run_job(cfg, client, job, model_override=os.environ.get("WHISPER_MODEL") or None)


def run_forever(cfg):
    if not (cfg.companion_url and cfg.worker_token):
        raise SystemExit("COMPANION_URL and WORKER_TOKEN must be set for the worker")
    client = Client(cfg.companion_url, cfg.worker_token, cfg.worker_name)
    log.info("worker %s polling %s every %ss (model %s, device %s)", cfg.worker_name, cfg.companion_url,
             cfg.poll_seconds, os.environ.get("WHISPER_MODEL") or "from companion", cfg.device)
    while True:
        try:
            res = run_once(cfg, client)
        except CompanionError as e:
            log.warning("%s", e)
            res = None
        if res is None:
            time.sleep(cfg.poll_seconds)
