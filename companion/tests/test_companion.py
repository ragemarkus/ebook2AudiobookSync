"""Companion tests: fake ABS API, fake transcriber (tests/fake_generator.py),
the real sidecar/validate_sidecar.py and publish_sidecar.abs_copy.

    python -m unittest discover -s companion/tests -v

Optional real run (faster-whisper installed, demo EPUB + MP3 present):
    REAL_GENERATOR_PYTHON=/path/to/venv/python REAL_DEMO_DIR=/path/with/epub+mp3 python -m unittest ...
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from readalong_companion import pipeline, worker                      # noqa: E402
from readalong_companion.abs_client import ABS, analyse               # noqa: E402
from readalong_companion.app import App                               # noqa: E402
from readalong_companion.config import Config, map_path, parse_path_map  # noqa: E402
from readalong_companion.store import Store                           # noqa: E402
from readalong_companion.web import make_server                       # noqa: E402

import fakes                                                          # noqa: E402

SIDECAR_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "sidecar")
SERVER_ROOT = "/audiobooks"      # library root as the (fake) ABS server sees it


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def http(method, url, token=None, data=None, headers=None):
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            body = resp.read()
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ra-companion-")
        self.epub_src = fakes.make_epub(os.path.join(self.tmp, "fixture.epub"))
        with open(self.epub_src, "rb") as f:
            self.abs = fakes.FakeABS(f.read()).start()
        self.lib = os.path.join(self.tmp, "library")   # the mounted ABS library folder
        os.makedirs(self.lib)
        self.genlog = os.path.join(self.tmp, "gen.log")
        os.environ["FAKE_GEN_LOG"] = self.genlog
        os.environ["FAKE_GEN_MODE"] = "ok"

    def tearDown(self):
        self.abs.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FAKE_GEN_LOG", "FAKE_GEN_MODE", "FAKE_GEN_SLEEP"):
            os.environ.pop(k, None)

    def cfg(self, mounted=True, **kw):
        c = Config(abs_url=self.abs.url, abs_token=fakes.TOKEN, cf_access_client_id=fakes.CF_ID,
                   cf_access_client_secret=fakes.CF_SECRET, data_dir=os.path.join(self.tmp, "data"),
                   sidecar_dir=SIDECAR_DIR, python=sys.executable,
                   generator_override=os.path.join(HERE, "fake_generator.py"),
                   path_map=parse_path_map("%s=%s" % (SERVER_ROOT, self.lib)) if mounted else [],
                   page_size=2, retry_base=600, scan_on_start=False)
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def add_book(self, item_id, title, sidecar=None, mounted_files=True, **kw):
        folder = SERVER_ROOT + "/A. Author/" + title
        item = fakes.book_item(item_id, title, folder, sidecar=sidecar, **kw)
        self.abs.add(item)
        local = os.path.join(self.lib, "A. Author", title)
        if mounted_files:
            os.makedirs(local, exist_ok=True)
            for a in item["media"]["audioFiles"]:
                with open(os.path.join(local, a["metadata"]["filename"]), "wb") as f:
                    f.write(b"LOCALAUDIO")
            if item["media"]["ebookFile"]:
                shutil.copyfile(self.epub_src, os.path.join(local, item["media"]["ebookFile"]["metadata"]["filename"]))
        return item, local

    def gen_calls(self):
        if not os.path.exists(self.genlog):
            return []
        with open(self.genlog) as f:
            return [json.loads(l) for l in f]


# ---------------------------------------------------------------- units
class TestUnits(unittest.TestCase):
    def test_analyse_full_and_minified(self):
        it = fakes.book_item("li_1", "T", "/a/T", sidecar="T.readalong.json")
        a = analyse(it)
        self.assertEqual((a["n_audio"], a["epub"], a["ebook_file"]), (2, True, "Fixture Book.epub"))
        self.assertEqual(a["sidecars"], ["T.readalong.json"])
        m = analyse(fakes.minified(it))
        self.assertEqual((m["n_audio"], m["epub"], m["ebook_file"], m["sidecars"]), (2, True, None, None))
        self.assertFalse(analyse(fakes.book_item("li_2", "P", "/a/P", epub="P.pdf"))["epub"])
        self.assertEqual(analyse(fakes.book_item("li_3", "E", "/a/E", audio=()))["n_audio"], 0)

    def test_path_map(self):
        pm = parse_path_map("/audiobooks=/library, /audiobooks/big=/big ,bad")
        self.assertEqual(map_path(pm, "/audiobooks/X/Y"), "/library/X/Y")
        self.assertEqual(map_path(pm, "/audiobooks/big/Z"), "/big/Z")
        self.assertIsNone(map_path(pm, "/audiobooksX/Y"))
        self.assertIsNone(map_path(pm, "/other/Y"))
        self.assertIsNone(map_path([], "/audiobooks/X"))

    def test_config_from_env(self):
        c = Config.from_env({"ABS_URL": "http://<abs-host>:13378/", "WORKER_MODE": "both", "WORKER_TOKEN": "w",
                             "ABS_PATH_MAP": "/audiobooks=/library", "ABS_SCAN_AFTER_PUBLISH": "true",
                             "WHISPER_MODEL": "medium.en"})
        self.assertEqual(c.abs_url, "http://<abs-host>:13378")
        self.assertTrue(c.local_worker_enabled and c.remote_workers_enabled and c.abs_scan_after_publish)
        self.assertEqual(c.model, "medium.en")
        self.assertNotIn("worker_token", repr(c))
        with self.assertRaises(ValueError):
            Config.from_env({"WORKER_MODE": "gpu"})

    def test_sidecar_name(self):
        self.assertEqual(pipeline.sidecar_name("My Book.EPUB"), "My Book.readalong.json")
        self.assertEqual(pipeline.safe_name("../../etc/passwd"), "passwd")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = Clock()
        self.s = Store(os.path.join(self.tmp, "s.db"), max_attempts=3, retry_base=600, clock=self.clock)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def seen(self, iid="li_1", has=False, upd=1):
        return self.s.upsert_seen(iid, "lib", "T", "A", "/p", "T.epub", upd, has)

    def test_retry_backoff_then_failed(self):
        self.assertEqual(self.seen(), "pending")
        r = self.s.claim("local", 900)
        self.assertEqual((r["status"], r["attempts"]), ("running", 1))
        self.assertIsNone(self.s.claim("local", 900))
        self.assertEqual(self.s.fail("li_1", "boom"), "pending")
        self.assertIsNone(self.s.claim("local", 900), "backoff not respected")
        self.clock.t += 601
        self.assertIsNotNone(self.s.claim("local", 900))
        self.assertEqual(self.s.fail("li_1", "boom"), "pending")
        self.assertEqual(self.s.get("li_1")["next_attempt_at"], self.clock.t + 1200)   # 600 * 2
        self.clock.t += 1201
        self.s.claim("local", 900)
        self.assertEqual(self.s.fail("li_1", "boom"), "failed")
        self.assertEqual(self.s.get("li_1")["last_error"], "boom")

    def test_review_not_retried_and_scan_transitions(self):
        self.seen()
        self.s.claim("local", 900)
        self.assertEqual(self.s.fail("li_1", "low match", retryable=False, review=True), "needs_review")
        self.assertEqual(self.seen(has=True), "existing")          # someone dropped a sidecar in
        self.assertEqual(self.seen(has=False), "pending")          # and removed it again
        self.assertEqual(self.seen("li_2", has=True), "existing")

    def test_lease_expiry_regenerate_restart(self):
        self.seen()
        self.s.claim("remote:g14", 900)
        self.assertTrue(self.s.heartbeat("li_1", "remote:g14", 900))
        self.assertFalse(self.s.heartbeat("li_1", "remote:other", 900))
        self.clock.t += 901
        self.assertEqual(self.s.requeue_expired(), 1)
        r = self.s.get("li_1")
        self.assertEqual(r["status"], "pending")
        self.assertIn("lease expired", r["last_error"])
        self.clock.t += 10000
        self.s.claim("local", 900)
        self.assertEqual(self.s.requeue_worker("local"), 1)       # restart: not counted
        self.assertEqual(self.s.get("li_1")["attempts"], 1)
        self.s.claim("local", 900)
        self.s.finish("li_1", "done")
        self.assertTrue(self.s.regenerate("li_1"))
        r = self.s.get("li_1")
        self.assertEqual((r["status"], r["force"], r["attempts"]), ("pending", 1, 0))
        self.assertEqual(self.seen(has=True, upd=2), "pending", "forced regenerate must survive a scan")

    def test_local_claim_delay(self):
        self.seen()
        self.assertIsNone(self.s.claim("local", 900, min_age=3600))
        self.clock.t += 3601
        self.assertIsNotNone(self.s.claim("local", 900, min_age=3600))


# ---------------------------------------------------------------- scanning
class TestScan(Base):
    def test_scan_queues_only_audio_plus_epub_without_sidecar(self):
        self.add_book("li_a", "Needs Sidecar")
        self.add_book("li_b", "Has Sidecar", sidecar="Has Sidecar.readalong.json", ino0=200)
        self.add_book("li_c", "Audio Only", epub=None, ino0=300)
        self.add_book("li_d", "PDF Book", epub="PDF Book.pdf", ino0=400)
        self.add_book("li_e", "Ebook Only", audio=(), ino0=500)
        app = App(self.cfg())
        s = app.rescan()
        self.assertEqual(s["errors"], [])
        self.assertEqual((s["libraries"], s["items"], s["eligible"], s["queued"], s["existing"]), (1, 5, 2, 1, 1))
        self.assertEqual((s["no_epub"], s["no_audio"]), (2, 1))
        self.assertEqual(app.store.get("li_a")["status"], "pending")
        self.assertEqual(app.store.get("li_b")["status"], "existing")
        self.assertIsNone(app.store.get("li_c"))
        # pagination with page_size=2 over 5 items: pages 0,1,2
        pages = [c for c in self.abs.calls if "/api/libraries/lib_books/items" in c[1]]
        self.assertEqual(len(pages), 3)
        # unchanged items are not fetched again
        n = len([c for c in self.abs.calls if c[1].startswith("/api/items/")])
        app.rescan()
        self.assertEqual(n, len([c for c in self.abs.calls if c[1].startswith("/api/items/")]))

    def test_cloudflare_headers_required_and_reported(self):
        self.add_book("li_a", "Needs Sidecar")
        app = App(self.cfg(cf_access_client_id=""))
        s = app.rescan()
        self.assertTrue(s["errors"] and "401" in s["errors"][0], s)
        self.assertNotIn(fakes.TOKEN, json.dumps(s))


# ---------------------------------------------------------------- local jobs
class TestLocalJobs(Base):
    def test_mounted_library_generates_validates_publishes(self):
        item, local = self.add_book("li_a", "Needs Sidecar")
        app = App(self.cfg(abs_scan_after_publish=True))
        app.rescan()
        self.assertEqual(app.local_worker_once(), "done")
        row = app.store.get("li_a")
        self.assertEqual(row["status"], "done", row)
        dest = os.path.join(local, "Fixture Book.readalong.json")
        self.assertEqual(row["published_path"], dest)
        sc = read_json(dest)
        self.assertEqual(sc["audio"]["abs_item_id"], "li_a")
        self.assertEqual([t["file"] for t in sc["audio"]["tracks"]], ["001-01 Intro.mp3", "002-02 Chapter.mp3"])
        self.assertEqual(row["stats"]["xpointers"], [2, 2])
        self.assertEqual(self.abs.scans, ["li_a"])
        # mounted: audio came from the folder, not downloaded through ABS
        self.assertFalse([c for c in self.abs.calls if "/download" in c[1]])
        argv = self.gen_calls()[0]
        self.assertIn("--abs-item-id", argv)
        self.assertFalse(os.path.exists(os.path.join(app.cfg.work_dir, "li_a")), "work dir not cleaned")
        self.assertIsNone(app.local_worker_once())
        # next scan sees the sidecar listed by ABS -> stays done
        self.abs.items["li_a"]["libraryFiles"].append({"ino": "999", "metadata": {"filename": "Fixture Book.readalong.json"}})
        self.abs.items["li_a"]["updatedAt"] = 2000
        app.rescan()
        self.assertEqual(app.store.get("li_a")["status"], "done")

    def test_download_mode_without_mount(self):
        self.add_book("li_a", "Needs Sidecar", mounted_files=False)
        app = App(self.cfg(mounted=False))
        app.rescan()
        self.assertEqual(app.local_worker_once(), "generated")
        row = app.store.get("li_a")
        self.assertIn("not mounted", row["last_error"])
        self.assertTrue(app.sidecar_path("li_a"))
        dl = [c for c in self.abs.calls if "/download" in c[1]]
        self.assertEqual(len(dl), 3)   # 2 tracks + EPUB, via ABS with CF headers

    def test_low_match_goes_to_review_and_is_not_published(self):
        item, local = self.add_book("li_a", "Needs Sidecar")
        os.environ["FAKE_GEN_MODE"] = "lowmatch"
        app = App(self.cfg())
        app.rescan()
        self.assertEqual(app.local_worker_once(), "needs_review")
        self.assertIn("wrong edition", app.store.get("li_a")["last_error"])
        self.assertFalse(os.path.exists(os.path.join(local, "Fixture Book.readalong.json")))
        self.assertIsNone(app.local_worker_once(), "review items must not be retried")

    def test_bad_xpointers_rejected_by_validate_sidecar(self):
        item, local = self.add_book("li_a", "Needs Sidecar")
        os.environ["FAKE_GEN_MODE"] = "badxp"
        app = App(self.cfg())
        app.rescan()
        self.assertEqual(app.local_worker_once(), "needs_review")
        self.assertIn("1/2 sentence xpointers", app.store.get("li_a")["last_error"])
        self.assertFalse(os.path.exists(os.path.join(local, "Fixture Book.readalong.json")))

    def test_generator_crash_retries_then_fails(self):
        self.add_book("li_a", "Needs Sidecar")
        os.environ["FAKE_GEN_MODE"] = "fail"
        clock = Clock(time.time())
        cfg = self.cfg(max_attempts=2)
        os.makedirs(cfg.data_dir, exist_ok=True)
        app = App(cfg, store=Store(cfg.db_path, 2, 600, clock=clock))
        app.rescan()
        self.assertEqual(app.local_worker_once(), "pending")
        self.assertIn("simulated generator crash", app.store.get("li_a")["last_error"])
        self.assertIsNone(app.local_worker_once())
        clock.t += 601
        self.assertEqual(app.local_worker_once(), "failed")

    def test_regenerate_overwrites_existing(self):
        item, local = self.add_book("li_b", "Has Sidecar", sidecar="Fixture Book.readalong.json")
        with open(os.path.join(local, "Fixture Book.readalong.json"), "w") as f:
            f.write("{}")
        app = App(self.cfg())
        app.rescan()
        self.assertEqual(app.store.get("li_b")["status"], "existing")
        self.assertIsNone(app.local_worker_once())
        self.assertTrue(app.regenerate("li_b"))
        self.assertEqual(app.local_worker_once(), "done")
        self.assertEqual(read_json(os.path.join(local, "Fixture Book.readalong.json"))["format"],
                         "koreader-readalong")


class TestBackgroundThreads(Base):
    def test_start_scans_and_local_worker_finishes(self):
        item, local = self.add_book("li_a", "Needs Sidecar")
        app = App(self.cfg(scan_on_start=True, poll_seconds=1))
        app.start()
        try:
            deadline = time.time() + 30
            while time.time() < deadline and (app.store.get("li_a") or {}).get("status") != "done":
                time.sleep(0.2)
        finally:
            app.shutdown()
        self.assertEqual(app.store.get("li_a")["status"], "done")
        self.assertTrue(os.path.exists(os.path.join(local, "Fixture Book.readalong.json")))


# ---------------------------------------------------------------- web + remote worker
class ServerBase(Base):
    def start_server(self, **kw):
        self.app = App(self.cfg(**kw))
        self.srv = make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
        super().tearDown()


class TestWeb(ServerBase):
    def test_api_and_ui(self):
        self.add_book("li_a", "Needs Sidecar")
        self.start_server(admin_token="admin-secret")
        st, body = http("GET", self.base + "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Read-along companion", body)
        self.assertEqual(http("POST", self.base + "/api/rescan")[0], 401)
        self.assertEqual(http("POST", self.base + "/api/rescan", "admin-secret")[0], 202)
        self.app.rescan()   # the scheduler thread isn't running in this test
        st, body = http("GET", self.base + "/api/queue")
        items = json.loads(body)["items"]
        self.assertEqual([(i["item_id"], i["status"]) for i in items], [("li_a", "pending")])
        st, body = http("GET", self.base + "/api/status")
        s = json.loads(body)
        self.assertEqual(s["counts"], {"pending": 1})
        self.assertNotIn("admin-secret", body.decode())
        self.assertNotIn(fakes.TOKEN, body.decode())
        self.assertEqual(http("GET", self.base + "/api/sidecar/li_a")[0], 404)
        self.assertEqual(http("GET", self.base + "/api/sidecar/..%2F..%2Fetc")[0], 400)
        self.app.local_worker_once()
        st, body = http("GET", self.base + "/api/sidecar/li_a")
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["audio"]["abs_item_id"], "li_a")
        st, body = http("GET", self.base + "/api/items/li_a")
        self.assertEqual(json.loads(body)["status"], "done")
        self.assertEqual(http("POST", self.base + "/api/items/li_a/regenerate", "admin-secret")[0], 202)
        self.assertEqual(self.app.store.get("li_a")["status"], "pending")
        self.assertEqual(http("POST", self.base + "/api/items/li_new/regenerate", "admin-secret")[0], 202)
        self.assertEqual(http("GET", self.base + "/api/events")[0], 200)
        # worker API is off in local mode
        self.assertEqual(http("POST", self.base + "/api/worker/claim", "x", b"{}")[0], 403)


class TestRemoteWorker(ServerBase):
    def wcfg(self):
        return Config(data_dir=os.path.join(self.tmp, "g14"), sidecar_dir=SIDECAR_DIR, python=sys.executable,
                      generator_override=os.path.join(HERE, "fake_generator.py"),
                      companion_url=self.base, worker_token="worker-secret", worker_name="g14", device="cuda")

    def test_g14_pulls_generates_uploads(self):
        item, local = self.add_book("li_a", "Needs Sidecar")
        self.start_server(worker_mode="remote", worker_token="worker-secret")
        self.app.rescan()
        self.assertEqual(http("POST", self.base + "/api/worker/claim", "wrong", b"{}")[0], 401)
        res = worker.run_once(self.wcfg())
        self.assertEqual(res["status"], "done", res)
        row = self.app.store.get("li_a")
        self.assertEqual((row["status"], row["stats"]["worker"]), ("done", "g14"))
        self.assertTrue(os.path.exists(os.path.join(local, "Fixture Book.readalong.json")))
        argv = self.gen_calls()[0]
        self.assertEqual(argv[argv.index("--device") + 1], "cuda")
        self.assertIsNone(worker.run_once(self.wcfg()), "queue should be empty")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "g14", "work", "li_a")))

    def test_worker_failure_requeues_and_file_proxy_is_scoped(self):
        self.add_book("li_a", "Needs Sidecar")
        self.add_book("li_b", "Other", ino0=200)
        self.start_server(worker_mode="both", worker_token="worker-secret", local_claim_delay=3600)
        self.app.rescan()
        self.assertIsNone(self.app.local_worker_once(), "XPS must leave fresh jobs for the G14")
        c = worker.Client(self.base, "worker-secret", "g14")
        job = c.claim()
        other = "li_b" if job["item_id"] == "li_a" else "li_a"
        h = {"X-Worker-Name": "g14"}
        self.assertEqual(http("GET", self.base + "/api/worker/jobs/%s/file/100" % other, "worker-secret", headers=h)[0], 409)
        self.assertEqual(http("GET", self.base + "/api/worker/jobs/%s/file/nope" % job["item_id"], "worker-secret",
                              headers=h)[0], 404)
        self.assertNotIn("local", json.dumps(job))
        self.assertNotIn(SERVER_ROOT, json.dumps(job))
        os.environ["FAKE_GEN_MODE"] = "fail"
        res = worker.run_job(self.wcfg(), c, job)
        self.assertEqual(res["status"], "pending")
        self.assertIn("worker g14", self.app.store.get(job["item_id"])["last_error"])


class TestReleaseBundle(unittest.TestCase):
    def test_deploy_bundle_is_pinned_and_has_no_build_section(self):
        import subprocess
        import tarfile
        out = tempfile.mkdtemp()
        try:
            script = os.path.join(os.path.dirname(HERE), "release", "make_release_assets.py")
            subprocess.run([sys.executable, script, "--version", "v1.2.3", "--out", out, "--skip-plugin"],
                           check=True, capture_output=True)
            name = "readalong-companion-deploy-1.2.3"
            with tarfile.open(os.path.join(out, name + ".tar.gz")) as t:
                files = {os.path.basename(m.name): t.extractfile(m).read().decode()
                         for m in t.getmembers() if m.isfile()}
            self.assertEqual(sorted(files), ["BUNDLE.md", "README.md", "docker-compose.worker.yml",
                                             "docker-compose.yml", "env.example", "worker.env.example"])
            for f in ("docker-compose.yml", "docker-compose.worker.yml"):
                self.assertNotIn("build:", files[f])
                self.assertNotIn("dockerfile", files[f])
                self.assertIn("ghcr.io/ragemarkus/readalong-companion:${COMPANION_TAG:-1.2.3}", files[f])
            self.assertIn("COMPANION_TAG=1.2.3", files["env.example"])
            sums = open(os.path.join(out, "SHA256SUMS")).read()
            self.assertIn(name + ".zip", sums)
            try:
                import yaml
            except ImportError:
                return
            c = yaml.safe_load(files["docker-compose.yml"])["services"]["readalong-companion"]
            self.assertNotIn("build", c)
        finally:
            shutil.rmtree(out, ignore_errors=True)


@unittest.skipUnless(os.environ.get("REAL_GENERATOR_PYTHON") and os.environ.get("REAL_DEMO_DIR"),
                     "real faster-whisper run not requested")
class TestRealGenerator(Base):
    def test_real_alignment_of_demo_book(self):
        demo = os.environ["REAL_DEMO_DIR"]
        epub = next(os.path.join(demo, f) for f in os.listdir(demo) if f.endswith(".epub"))
        mp3 = next(os.path.join(demo, f) for f in os.listdir(demo) if f.endswith(".mp3"))
        title = "Gift of the Magi"
        folder = SERVER_ROOT + "/O. Henry/" + title
        item = fakes.book_item("li_magi", title, folder, audio=(os.path.basename(mp3),), epub=os.path.basename(epub))
        self.abs.add(item)
        local = os.path.join(self.lib, "O. Henry", title)
        os.makedirs(local)
        os.symlink(os.path.realpath(mp3), os.path.join(local, os.path.basename(mp3)))
        shutil.copyfile(epub, os.path.join(local, os.path.basename(epub)))
        cfg = self.cfg(generator_override="", python=os.environ["REAL_GENERATOR_PYTHON"],
                       model=os.environ.get("REAL_MODEL", "base.en"), device="cpu")
        app = App(cfg)
        app.rescan()
        self.assertEqual(app.local_worker_once(), "done", app.store.get("li_magi")["last_error"])
        row = app.store.get("li_magi")
        print("\nreal run:", json.dumps({"stats": row["stats"]["stats"], "xpointers": row["stats"]["xpointers"]}))
        self.assertGreater(row["stats"]["stats"]["matched_in_span"], 0.9)


if __name__ == "__main__":
    unittest.main()
