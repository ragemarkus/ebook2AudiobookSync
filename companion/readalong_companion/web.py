"""Tiny web UI + JSON API (stdlib http.server).

Read endpoints are open on the LAN; POSTs need ADMIN_TOKEN when it is set;
/api/worker/* always needs WORKER_TOKEN.  Don't publish this port to the
internet."""
import hmac
import json
import re
import shutil
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import ITEM_ID

WORKER_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MAX_UPLOAD = 200 * 1024 * 1024

UI = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Read-along companion</title>
<style>
body{font:15px system-ui,sans-serif;margin:1.2rem;color:#222;background:#fafafa}
h1{font-size:1.3rem;margin:0 0 .6rem} table{border-collapse:collapse;width:100%;background:#fff}
th,td{border-bottom:1px solid #e3e3e3;padding:.35rem .5rem;text-align:left;vertical-align:top}
th{background:#f0f0f0} .chip{display:inline-block;padding:.05rem .45rem;border-radius:.6rem;font-size:.8rem;background:#ddd}
.done,.existing{background:#cdeccd}.running{background:#cfe3ff}.pending{background:#fff1bf}
.failed,.needs_review{background:#ffd0cc}.generated{background:#e6dcff}
button{font:inherit;padding:.25rem .7rem;cursor:pointer} .err{color:#a00;font-size:.8rem;white-space:pre-wrap;max-width:40rem}
#counts span{margin-right:.8rem} small{color:#666} .bar{display:flex;gap:.6rem;align-items:center;margin:.6rem 0;flex-wrap:wrap}
</style></head><body>
<h1>Read-along companion</h1>
<div id="counts"></div><div class="bar"><button onclick="rescan()">Rescan ABS</button>
<select id="filter" onchange="load()"><option value="">all</option><option>pending</option><option>running</option>
<option>done</option><option>generated</option><option>existing</option><option>needs_review</option><option>failed</option></select>
<small id="scan"></small></div>
<table><thead><tr><th>Book</th><th>Status</th><th>Tries</th><th>Info</th><th></th></tr></thead><tbody id="rows"></tbody></table>
<h2 style="font-size:1rem">Recent events</h2><div id="events"></div>
<script>
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tok=()=>localStorage.getItem("ra_admin")||"";
async function post(u){let r=await fetch(u,{method:"POST",headers:{Authorization:"Bearer "+tok()}});
 if(r.status==401){const t=prompt("Admin token");if(t){localStorage.setItem("ra_admin",t);return post(u)}}
 if(!r.ok)alert(u+": HTTP "+r.status);load()}
const rescan=()=>post("api/rescan");
const regen=id=>{if(confirm("Regenerate this sidecar?"))post("api/items/"+encodeURIComponent(id)+"/regenerate")};
const t=s=>s?new Date(s*1000).toLocaleString():"";
async function load(){
 const st=await (await fetch("api/status")).json();
 document.getElementById("counts").innerHTML=Object.entries(st.counts).map(([k,v])=>`<span><span class="chip ${esc(k)}">${esc(k)}</span> ${v}</span>`).join("")
  +`<small>mode: ${esc(st.config.worker_mode)}${st.config.remote_workers?" (remote workers on)":""}, model ${esc(st.config.model)}</small>`;
 const ls=st.last_scan;document.getElementById("scan").textContent=st.scanning?"scanning…":ls?`last scan ${t(ls.finished_at)}: ${ls.items} items, ${ls.eligible} with audio+EPUB${ls.errors&&ls.errors.length?", errors: "+ls.errors[0]:""}`:"not scanned yet";
 const f=document.getElementById("filter").value;
 const q=await (await fetch("api/queue"+(f?"?status="+f:""))).json();
 document.getElementById("rows").innerHTML=q.items.map(i=>`<tr><td><b>${esc(i.title)}</b><br><small>${esc(i.author)} · ${esc(i.item_id)}</small></td>
 <td><span class="chip ${esc(i.status)}">${esc(i.status)}</span>${i.worker?"<br><small>"+esc(i.worker)+"</small>":""}</td><td>${i.attempts}</td>
 <td>${i.stats&&i.stats.stats?`<small>matched ${(100*(i.stats.stats.matched_in_span||0)).toFixed(1)}%, ${i.stats.stats.cues||"?"} cues</small><br>`:""}
 ${i.next_attempt_at>Date.now()/1000?`<small>retry ${t(i.next_attempt_at)}</small><br>`:""}<div class="err">${esc(i.last_error)}</div></td>
 <td>${["done","generated"].includes(i.status)?`<a href="api/sidecar/${encodeURIComponent(i.item_id)}">sidecar</a> `:""}
 ${i.status!="running"?`<button onclick="regen('${esc(i.item_id)}')">Regenerate</button>`:""}</td></tr>`).join("")||"<tr><td colspan=5>nothing yet</td></tr>";
 const ev=await (await fetch("api/events?limit=15")).json();
 document.getElementById("events").innerHTML=ev.events.map(e=>`<div><small>${t(e.ts)}</small> ${e.item_id?"<small>"+esc(e.item_id)+"</small> ":""}${esc(e.msg)}</div>`).join("");
}
load();setInterval(load,10000);
</script></body></html>"""


def make_handler(app):
    cfg = app.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "readalong-companion"

        def log_message(self, fmt, *args):  # no request logging (no tokens in URLs anyway)
            pass

        # -------------------------------------------------- responses
        def _send(self, code, obj=None, ctype="application/json", body=None, extra=None):
            if body is None:
                body = json.dumps(obj if obj is not None else {}, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _bearer(self):
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.lower().startswith("bearer ") else self.headers.get("X-Admin-Token", "")

        def _admin_ok(self):
            if not cfg.admin_token:
                return True
            if hmac.compare_digest(self._bearer().encode(), cfg.admin_token.encode()):
                return True
            self._send(401, {"error": "admin token required"})
            return False

        def _worker(self):
            if not cfg.remote_workers_enabled:
                self._send(403, {"error": "remote workers disabled (WORKER_MODE=remote|both and WORKER_TOKEN)"})
                return None
            if not hmac.compare_digest(self._bearer().encode(), cfg.worker_token.encode()):
                self._send(401, {"error": "bad worker token"})
                return None
            name = self.headers.get("X-Worker-Name", "worker")
            if not WORKER_NAME.match(name):
                self._send(400, {"error": "bad X-Worker-Name"})
                return None
            return name

        def _body(self, limit=1 << 20):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("body too large")
            return self.rfile.read(n) if n else b""

        def _route(self):
            u = urllib.parse.urlsplit(self.path)
            return [p for p in u.path.split("/") if p], urllib.parse.parse_qs(u.query)

        # -------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            parts, q = self._route()
            try:
                if not parts:
                    return self._send(200, body=UI.encode(), ctype="text/html; charset=utf-8")
                if parts[0] != "api":
                    return self._send(404, {"error": "not found"})
                p = parts[1:]
                if p == ["health"]:
                    return self._send(200, {"ok": True})
                if p == ["status"]:
                    return self._send(200, app.status())
                if p == ["queue"]:
                    st = (q.get("status") or [None])[0]
                    return self._send(200, {"items": app.store.list(st)})
                if p == ["events"]:
                    lim = min(int((q.get("limit") or ["50"])[0]), 500)
                    return self._send(200, {"events": app.store.events(lim)})
                if len(p) == 2 and p[0] == "items":
                    if not ITEM_ID.match(p[1]):
                        return self._send(400, {"error": "bad item id"})
                    row = app.store.get(p[1])
                    if not row:
                        return self._send(404, {"error": "unknown item"})
                    return self._send(200, dict(row, events=app.store.events(30, p[1])))
                if len(p) == 2 and p[0] == "sidecar":
                    iid = p[1][:-len(".readalong.json")] if p[1].endswith(".readalong.json") else p[1]
                    if not ITEM_ID.match(iid):
                        return self._send(400, {"error": "bad item id"})
                    path = app.sidecar_path(iid)
                    if not path:
                        return self._send(404, {"error": "no sidecar for this item"})
                    row = app.store.get(iid) or {}
                    name = (row.get("sidecar_file") or iid + ".readalong.json").replace('"', "'")
                    with open(path, "rb") as f:
                        data = f.read()
                    return self._send(200, body=data, ctype="application/json",
                                      extra={"Content-Disposition": 'inline; filename="%s"' % name})
                if len(p) == 5 and p[:2] == ["worker", "jobs"] and p[3] == "file":
                    w = self._worker()
                    if not w:
                        return
                    if not ITEM_ID.match(p[2]) or not app.worker_owns(p[2], w):
                        return self._send(409, {"error": "job not held by this worker"})
                    fobj, size = app.worker_open_file(p[2], w, p[4])
                    if fobj is None:
                        return self._send(404, {"error": "file is not part of this job"})
                    with fobj:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        if size is not None:
                            self.send_header("Content-Length", str(size))
                        self.end_headers()
                        shutil.copyfileobj(fobj, self.wfile, 1 << 20)
                    return
                return self._send(404, {"error": "not found"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

        # -------------------------------------------------- POST
        def do_POST(self):
            parts, q = self._route()
            p = parts[1:] if parts[:1] == ["api"] else None
            try:
                if p is None:
                    return self._send(404, {"error": "not found"})
                if p and p[0] == "worker":
                    return self._worker_post(p)
                if not self._admin_ok():
                    return
                if p == ["rescan"]:
                    app.request_scan()
                    return self._send(202, {"scan": "requested"})
                if len(p) == 3 and p[0] == "items" and p[2] == "regenerate":
                    if not ITEM_ID.match(p[1]):
                        return self._send(400, {"error": "bad item id"})
                    if not app.regenerate(p[1]):
                        return self._send(409, {"error": "item is running"})
                    return self._send(202, {"item_id": p[1], "status": "pending"})
                return self._send(404, {"error": "not found"})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:
                return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

        def _worker_post(self, p):
            w = self._worker()
            if not w:
                return
            if p == ["worker", "claim"]:
                job = app.worker_claim(w)
                return self._send(200, {"job": job}) if job else self._send(204, body=b"")
            if len(p) != 4 or p[1] != "jobs" or not ITEM_ID.match(p[2]):
                return self._send(404, {"error": "not found"})
            iid, action = p[2], p[3]
            if not app.worker_owns(iid, w):
                return self._send(409, {"error": "job not held by this worker (lease expired?)"})
            if action == "heartbeat":
                return self._send(200, {"ok": app.worker_heartbeat(iid, w)})
            if action == "complete":
                body = self._body(MAX_UPLOAD)
                return self._send(200, app.worker_complete(iid, w, body))
            if action == "fail":
                d = json.loads(self._body() or b"{}")
                st = app.worker_fail(iid, w, str(d.get("error", "unknown"))[:4000],
                                     bool(d.get("retryable", True)), bool(d.get("review", False)))
                return self._send(200, {"status": st})
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(app, host=None, port=None):
    srv = ThreadingHTTPServer((host if host is not None else app.cfg.host,
                               port if port is not None else app.cfg.port), make_handler(app))
    srv.daemon_threads = True
    return srv
