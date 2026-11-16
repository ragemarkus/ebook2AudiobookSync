"""Persistent queue and state in SQLite.

Item status:
  pending      queued (or waiting for a retry: next_attempt_at)
  running      claimed by a worker that holds a lease
  done         generated, validated and written into the ABS item folder
  generated    generated and validated, kept here only (no folder mapping)
  existing     ABS already lists a *.readalong.json for the item
  needs_review generated but failed the quality checks (wrong edition?); not retried
  failed       gave up after MAX_ATTEMPTS
"""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  item_id TEXT PRIMARY KEY,
  library_id TEXT, title TEXT, author TEXT, server_path TEXT, ebook_file TEXT,
  abs_updated_at INTEGER DEFAULT 0,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  queued_at REAL,
  force INTEGER NOT NULL DEFAULT 0,
  worker TEXT, lease_until REAL, started_at REAL, finished_at REAL,
  sidecar_file TEXT, published_path TEXT, stats TEXT,
  created_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, item_id TEXT, level TEXT, msg TEXT
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

ACTIVE = ("pending", "running")


class Store:
    def __init__(self, path, max_attempts=3, retry_base=600, clock=time.time):
        self.path = path
        self.max_attempts = max_attempts
        self.retry_base = retry_base
        self.clock = clock
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript(SCHEMA)

    # ------------------------------------------------------------ helpers
    def _x(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args)

    def get(self, item_id):
        r = self._x("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        return self._row(r)

    @staticmethod
    def _row(r):
        if r is None:
            return None
        d = dict(r)
        if d.get("stats"):
            try:
                d["stats"] = json.loads(d["stats"])
            except ValueError:
                pass
        return d

    def event(self, item_id, msg, level="info"):
        self._x("INSERT INTO events(ts,item_id,level,msg) VALUES(?,?,?,?)", (self.clock(), item_id, level, msg[:2000]))
        self._x("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 2000")

    def events(self, limit=50, item_id=None):
        if item_id:
            rows = self._x("SELECT * FROM events WHERE item_id=? ORDER BY id DESC LIMIT ?", (item_id, limit))
        else:
            rows = self._x("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def set_meta(self, k, v):
        self._x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def get_meta(self, k, default=None):
        r = self._x("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    # ------------------------------------------------------------ scanning
    def upsert_seen(self, item_id, library_id, title, author, server_path, ebook_file, updated_at, has_sidecar):
        now = self.clock()
        with self._lock:
            cur = self.get(item_id)
            if cur is None:
                status = "existing" if has_sidecar else "pending"
                self._x("INSERT INTO items(item_id,library_id,title,author,server_path,ebook_file,abs_updated_at,"
                        "status,queued_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (item_id, library_id, title, author, server_path, ebook_file, updated_at,
                         status, now, now, now))
                if status == "pending":
                    self.event(item_id, "queued: %s" % title)
                return status
            status = cur["status"]
            new = status
            if has_sidecar and status in ("pending", "failed", "needs_review") and not cur["force"]:
                new = "existing"
            elif not has_sidecar and status == "existing":
                new = "pending"          # sidecar was removed from the item: make it again
            self._x("UPDATE items SET library_id=?,title=?,author=?,server_path=?,ebook_file=?,abs_updated_at=?,"
                    "status=?,updated_at=?, attempts=CASE WHEN ?='pending' AND status!='pending' THEN 0 ELSE attempts END,"
                    "queued_at=CASE WHEN ?='pending' AND status!='pending' THEN ? ELSE queued_at END "
                    "WHERE item_id=?",
                    (library_id, title, author, server_path, ebook_file, updated_at, new, now, new, new, now, item_id))
            if new != status:
                self.event(item_id, "%s -> %s (ABS scan)" % (status, new))
            return new

    # ------------------------------------------------------------ queue
    def claim(self, worker, lease_seconds, min_age=0):
        """Atomically take the next due job.  Returns the item row or None."""
        now = self.clock()
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                r = self.db.execute(
                    "SELECT item_id FROM items WHERE status='pending' AND next_attempt_at<=? AND "
                    "(force=1 OR COALESCE(queued_at,0)<=?) ORDER BY force DESC, queued_at, created_at LIMIT 1",
                    (now, now - min_age)).fetchone()
                if r is None:
                    self.db.execute("COMMIT")
                    return None
                self.db.execute("UPDATE items SET status='running', worker=?, lease_until=?, started_at=?,"
                                " attempts=attempts+1, updated_at=? WHERE item_id=?",
                                (worker, now + lease_seconds, now, now, r["item_id"]))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise
            row = self.get(r["item_id"])
        self.event(row["item_id"], "claimed by %s (attempt %d)" % (worker, row["attempts"]))
        return row

    def heartbeat(self, item_id, worker, lease_seconds):
        c = self._x("UPDATE items SET lease_until=?, updated_at=? WHERE item_id=? AND status='running' AND worker=?",
                    (self.clock() + lease_seconds, self.clock(), item_id, worker))
        return c.rowcount == 1

    def owns(self, item_id, worker):
        r = self.get(item_id)
        return bool(r and r["status"] == "running" and r["worker"] == worker)

    def finish(self, item_id, status, sidecar_file=None, published_path=None, stats=None, note=None):
        now = self.clock()
        self._x("UPDATE items SET status=?, sidecar_file=?, published_path=?, stats=?, last_error=?, worker=NULL,"
                " lease_until=NULL, finished_at=?, force=0, updated_at=? WHERE item_id=?",
                (status, sidecar_file, published_path, json.dumps(stats) if stats is not None else None,
                 note, now, now, item_id))
        self.event(item_id, "%s%s" % (status, (": " + note) if note else ""))

    def fail(self, item_id, error, retryable=True, review=False):
        """Record a failed attempt.  Retryable failures back off exponentially
        until MAX_ATTEMPTS; quality failures go to needs_review."""
        now = self.clock()
        with self._lock:
            r = self.get(item_id)
            if r is None:
                return None
            if review:
                status, nxt = "needs_review", 0
            elif retryable and r["attempts"] < self.max_attempts:
                status, nxt = "pending", now + self.retry_base * (2 ** max(r["attempts"] - 1, 0))
            else:
                status, nxt = "failed", 0
            self._x("UPDATE items SET status=?, last_error=?, next_attempt_at=?, worker=NULL, lease_until=NULL,"
                    " finished_at=?, updated_at=? WHERE item_id=?",
                    (status, error[-4000:], nxt, now, now, item_id))
        self.event(item_id, "attempt %d failed -> %s: %s" % (r["attempts"], status, error[-300:]),
                   "error" if status != "pending" else "warn")
        return status

    def requeue_expired(self):
        """Jobs whose worker stopped heartbeating go back to the queue."""
        now = self.clock()
        rows = self._x("SELECT item_id, worker FROM items WHERE status='running' AND lease_until<?", (now,)).fetchall()
        for r in rows:
            self.fail(r["item_id"], "lease expired (worker %s stopped responding)" % r["worker"], retryable=True)
        return len(rows)

    def requeue_worker(self, worker):
        """On restart: jobs this process held are re-queued without counting the attempt."""
        rows = self._x("SELECT item_id FROM items WHERE status='running' AND worker=?", (worker,)).fetchall()
        for r in rows:
            self._x("UPDATE items SET status='pending', attempts=MAX(attempts-1,0), worker=NULL, lease_until=NULL"
                    " WHERE item_id=?", (r["item_id"],))
            self.event(r["item_id"], "re-queued after restart")
        return len(rows)

    def regenerate(self, item_id):
        now = self.clock()
        c = self._x("UPDATE items SET status='pending', force=1, attempts=0, next_attempt_at=0, last_error=NULL,"
                    " queued_at=?, updated_at=? WHERE item_id=? AND status!='running'", (now, now, item_id))
        if c.rowcount:
            self.event(item_id, "regenerate requested")
        return c.rowcount == 1

    def add_manual(self, item_id, title=""):
        now = self.clock()
        self._x("INSERT OR IGNORE INTO items(item_id,title,status,force,queued_at,created_at,updated_at)"
                " VALUES(?,?,'pending',1,?,?,?)", (item_id, title or item_id, now, now, now))

    def list(self, status=None, limit=500):
        if status:
            st = [s for s in status.split(",") if s]
            rows = self._x("SELECT * FROM items WHERE status IN (%s) ORDER BY updated_at DESC LIMIT ?"
                           % ",".join("?" * len(st)), (*st, limit))
        else:
            rows = self._x("SELECT * FROM items ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1"
                           " ELSE 2 END, updated_at DESC LIMIT ?", (limit,))
        return [self._row(r) for r in rows]

    def counts(self):
        return {r["status"]: r["n"] for r in self._x("SELECT status, COUNT(*) n FROM items GROUP BY status")}
