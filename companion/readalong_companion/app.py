"""The companion service: scanner, queue, local CPU worker and the hooks the
web API and remote GPU workers use."""
import json
import logging
import os
import re
import shutil
import threading
import time

from . import pipeline
from .abs_client import ABS, ABSError, analyse
from .store import Store

log = logging.getLogger("readalong")
ITEM_ID = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")
LOCAL = "local"


class App:
    def __init__(self, cfg, abs_client=None, store=None):
        self.cfg = cfg
        os.makedirs(cfg.data_dir, exist_ok=True)
        os.makedirs(cfg.work_dir, exist_ok=True)
        os.makedirs(cfg.sidecars_dir, exist_ok=True)
        self.abs = abs_client or ABS.from_config(cfg)
        self.store = store or Store(cfg.db_path, cfg.max_attempts, cfg.retry_base)
        self.stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._scan_wake = threading.Event()
        self.scanning = False
        self.current = {}          # item_id -> worker, for the status page
        self._specs = {}           # item_id -> job spec of jobs handed to remote workers
        self.threads = []

    # ------------------------------------------------------------ scanning
    def rescan(self):
        if not self._scan_lock.acquire(blocking=False):
            return {"skipped": "scan already running"}
        self.scanning = True
        t0 = time.time()
        summary = {"libraries": 0, "items": 0, "eligible": 0, "queued": 0, "existing": 0,
                   "no_epub": 0, "no_audio": 0, "errors": []}
        try:
            libs = [l for l in self.abs.libraries() if l.get("mediaType", "book") == "book"
                    and (not self.cfg.library_ids or l.get("id") in self.cfg.library_ids)]
            for lib in libs:
                summary["libraries"] += 1
                for it in self.abs.library_items(lib["id"]):
                    summary["items"] += 1
                    try:
                        self._scan_item(lib["id"], it, summary)
                    except ABSError as e:
                        summary["errors"].append("%s: %s" % (it.get("id"), e))
        except ABSError as e:
            summary["errors"].append(str(e))
            self.store.event(None, "scan failed: %s" % e, "error")
        finally:
            summary["seconds"] = round(time.time() - t0, 1)
            summary["finished_at"] = time.time()
            self.store.set_meta("last_scan", summary)
            self.scanning = False
            self._scan_lock.release()
        if not summary["errors"]:
            self.store.event(None, "scan: %(items)d items, %(eligible)d with audio+EPUB, %(queued)d queued" % summary)
        return summary

    def _scan_item(self, library_id, it, summary):
        a = analyse(it)
        if a["missing"]:
            return
        if not a["n_audio"]:
            summary["no_audio"] += 1
            return
        if not a["epub"]:
            summary["no_epub"] += 1
            return
        summary["eligible"] += 1
        cur = self.store.get(it["id"])
        if cur and a["updated_at"] and cur["abs_updated_at"] == a["updated_at"]:
            # unchanged since the last look (adding/removing a file bumps updatedAt)
            if cur["status"] in ("pending", "running"):
                summary["queued"] += 1
            elif cur["status"] == "existing":
                summary["existing"] += 1
            return
        full = it
        if a["sidecars"] is None or a["ebook_file"] is None:
            full = self.abs.item(it["id"])   # minified listing: need libraryFiles + ebookFile
            a = analyse(full)
            if not (a["n_audio"] and a["epub"]):
                return
        status = self.store.upsert_seen(it["id"], library_id, a["title"], a["author"], full.get("path"),
                                        a["ebook_file"], a["updated_at"], bool(a["sidecars"]))
        if status == "pending":
            summary["queued"] += 1
        elif status == "existing":
            summary["existing"] += 1

    def request_scan(self):
        self._scan_wake.set()

    # ------------------------------------------------------------ job helpers
    def _spec(self, item_id):
        item = self.abs.item(item_id)
        return pipeline.job_spec(item, self.cfg.path_map)

    def _fetch_from_abs(self, item_id):
        return lambda ino, dest: self.abs.download(item_id, ino, dest)

    def _workdir(self, item_id):
        d = os.path.join(self.cfg.work_dir, item_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _cleanup(self, item_id):
        if not self.cfg.keep_work:
            shutil.rmtree(os.path.join(self.cfg.work_dir, item_id), ignore_errors=True)

    def _already_has_sidecar(self, row, spec):
        if spec["sidecars"] and not row.get("force"):
            self.store.finish(spec["item_id"], "existing", note="ABS already lists " + ", ".join(spec["sidecars"]))
            return True
        return False

    def _finish_ok(self, item_id, spec, sidecar_path, summary):
        status, dest = pipeline.publish(self.cfg, spec, sidecar_path, self.abs,
                                        log=lambda m: self.store.event(item_id, m))
        self.store.finish(item_id, status, sidecar_file=os.path.basename(sidecar_path), published_path=dest,
                          stats=summary, note=None if status == "done" else "not written to ABS (folder not mounted)")
        return status

    # ------------------------------------------------------------ local worker
    def run_local_job(self, row):
        item_id = row["item_id"]
        self.current[item_id] = LOCAL
        try:
            spec = self._spec(item_id)
            if self._already_has_sidecar(row, spec):
                return "existing"
            wd = self._workdir(item_id)
            epub, audio = pipeline.materialise(spec, wd, self._fetch_from_abs(item_id))
            hb = lambda: self.store.heartbeat(item_id, LOCAL, self.cfg.lease_seconds)
            out = pipeline.generate(self.cfg, spec, epub, audio, wd, heartbeat=hb)
            summary = pipeline.validate(self.cfg, out, item_id, os.path.join(wd, "audio"))
            status = self._finish_ok(item_id, spec, out, summary)
            self._cleanup(item_id)
            return status
        except pipeline.JobError as e:
            return self.store.fail(item_id, str(e), e.retryable, e.review)
        except ABSError as e:
            return self.store.fail(item_id, str(e), retryable=True)
        except Exception as e:  # never kill the worker thread
            log.exception("job %s", item_id)
            return self.store.fail(item_id, "%s: %s" % (type(e).__name__, e), retryable=True)
        finally:
            self.current.pop(item_id, None)

    def local_worker_once(self):
        row = self.store.claim(LOCAL, self.cfg.lease_seconds,
                               min_age=self.cfg.local_claim_delay if self.cfg.worker_mode == "both" else 0)
        if row is None:
            return None
        return self.run_local_job(row)

    # ------------------------------------------------------------ remote workers
    def worker_claim(self, worker):
        while True:
            row = self.store.claim("remote:" + worker, self.cfg.lease_seconds)
            if row is None:
                return None
            item_id = row["item_id"]
            try:
                spec = self._spec(item_id)
                if self._already_has_sidecar(row, spec):
                    continue
            except pipeline.JobError as e:
                self.store.fail(item_id, str(e), e.retryable, e.review)
                continue
            except ABSError as e:
                self.store.fail(item_id, str(e), retryable=True)
                return None
            self.current[item_id] = "remote:" + worker
            job = pipeline.public_spec(spec)
            job.update({"attempt": row["attempts"], "lease_seconds": self.cfg.lease_seconds,
                        "model": self.cfg.model, "language": self.cfg.language})
            self._specs[item_id] = spec
            return job

    def _spec_cached(self, item_id):
        spec = self._specs.get(item_id)
        if spec is None:
            spec = self._spec(item_id)
            self._specs[item_id] = spec
        return spec

    def worker_owns(self, item_id, worker):
        return self.store.owns(item_id, "remote:" + worker)

    def worker_heartbeat(self, item_id, worker):
        return self.store.heartbeat(item_id, "remote:" + worker, self.cfg.lease_seconds)

    def worker_open_file(self, item_id, worker, ino):
        """(fileobj, size) for one of the job's files: mounted copy or ABS."""
        spec = self._spec_cached(item_id)
        entry = next((f for f in spec["audio"] + [spec["ebook"]] if str(f["ino"]) == str(ino)), None)
        if entry is None:
            return None, None
        if entry.get("local") and os.path.isfile(entry["local"]):
            return open(entry["local"], "rb"), os.path.getsize(entry["local"])
        r = self.abs.open_file(item_id, ino)
        size = r.headers.get("Content-Length")
        return r, int(size) if size else None

    def worker_complete(self, item_id, worker, body):
        """A remote worker uploaded a sidecar: validate it here too, then publish."""
        try:
            spec = self._spec_cached(item_id)
            wd = os.path.join(self._workdir(item_id), "incoming")
            os.makedirs(wd, exist_ok=True)
            # the EPUB must sit next to the sidecar for validate_sidecar.py's xpointer check
            ebook_only = dict(spec, audio=[])
            epub, _ = pipeline.materialise(ebook_only, wd, self._fetch_from_abs(item_id))
            out = os.path.join(wd, pipeline.sidecar_name(spec["ebook"]["filename"]))
            with open(out, "wb") as f:
                f.write(body)
            summary = pipeline.validate(self.cfg, out, item_id, os.path.join(wd, "audio"))
            summary["worker"] = worker
            status = self._finish_ok(item_id, spec, out, summary)
            self._cleanup(item_id)
            return {"status": status}
        except pipeline.JobError as e:
            return {"status": self.store.fail(item_id, "uploaded sidecar rejected: %s" % e, e.retryable, e.review),
                    "error": str(e)}
        except ABSError as e:
            return {"status": self.store.fail(item_id, str(e), retryable=True), "error": str(e)}
        finally:
            self.current.pop(item_id, None)
            self._specs.pop(item_id, None)

    def worker_fail(self, item_id, worker, error, retryable=True, review=False):
        self.current.pop(item_id, None)
        self._specs.pop(item_id, None)
        return self.store.fail(item_id, "worker %s: %s" % (worker, error), retryable, review)

    # ------------------------------------------------------------ API helpers
    def regenerate(self, item_id):
        if not self.store.get(item_id):
            self.store.add_manual(item_id)
            return True
        return self.store.regenerate(item_id)

    def sidecar_path(self, item_id):
        p = os.path.join(self.cfg.sidecars_dir, "%s.readalong.json" % item_id)
        return p if os.path.isfile(p) else None

    def status(self):
        c = self.cfg
        return {
            "version": __import__("readalong_companion").__version__,
            "counts": self.store.counts(),
            "running": [{"item_id": k, "worker": v} for k, v in list(self.current.items())],
            "scanning": self.scanning,
            "last_scan": self.store.get_meta("last_scan"),
            "config": {"abs_url_set": bool(c.abs_url), "cf_access": bool(c.cf_access_client_id),
                       "worker_mode": c.worker_mode, "remote_workers": c.remote_workers_enabled,
                       "local_claim_delay": c.local_claim_delay, "model": c.model, "device": c.device,
                       "folder_mapping": bool(c.path_map), "scan_interval": c.scan_interval,
                       "abs_scan_after_publish": c.abs_scan_after_publish,
                       "min_match": c.min_match, "max_attempts": c.max_attempts},
        }

    # ------------------------------------------------------------ threads
    def _loop(self, name, fn, every):
        def run():
            while not self.stop.is_set():
                try:
                    busy = fn()
                except Exception:
                    log.exception("%s loop", name)
                    busy = None
                if not busy:
                    self.stop.wait(every)
        t = threading.Thread(target=run, name=name, daemon=True)
        t.start()
        self.threads.append(t)

    def _scheduler(self):
        first = [self.cfg.scan_on_start]

        def tick():
            due = first[0] or self._scan_wake.is_set()
            ls = self.store.get_meta("last_scan") or {}
            age = time.time() - ls.get("finished_at", 0)
            if self.cfg.scan_interval > 0 and age >= self.cfg.scan_interval:
                due = True
            if ls.get("errors") and not ls.get("libraries") and age >= 300:  # ABS unreachable: retry in 5 min
                due = True
            if due and self.abs.configured:
                first[0] = False
                self._scan_wake.clear()
                self.rescan()
            return None
        return tick

    def start(self):
        self.store.requeue_worker(LOCAL)
        self._loop("scanner", self._scheduler(), 2)
        self._loop("reaper", self.store.requeue_expired, 60)
        if self.cfg.local_worker_enabled:
            self._loop("local-worker", self.local_worker_once, self.cfg.poll_seconds)

    def shutdown(self):
        self.stop.set()
