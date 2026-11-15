"""Offline test of publish_sidecar.py against a fake ABS + Grimmory server.
    python tests/test_publish_sidecar.py
"""
import http.server, json, os, sys, tempfile, threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import publish_sidecar as P

calls = []
state = {"listed": False, "uploaded": None}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        calls.append(("POST", self.path, self.headers.get("Authorization"), self.headers.get("CF-Access-Client-Id")))
        if self.path == "/api/items/li_1/scan":
            state["listed"] = True
            return self._send({"result": "UPDATED"})
        if self.path == "/api/v1/auth/login":
            d = json.loads(body)
            assert d == {"username": "mark", "password": "pw"}, d
            return self._send({"accessToken": "jwt123"})
        if self.path.startswith("/api/v1/books/7/files?isBook=false"):
            assert self.headers["Authorization"] == "Bearer jwt123"
            assert b'name="file"; filename="Magi.readalong.json"' in body
            state["uploaded"] = body
            return self._send({"id": 99})
        self._send({}, 404)

    def do_GET(self):
        calls.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path == "/api/items/li_1?expanded=1":
            lf = [{"metadata": {"filename": "01.mp3"}}]
            if state["listed"]:
                lf.append({"metadata": {"filename": "Magi.readalong.json"}})
            return self._send({"libraryFiles": lf})
        if self.path == "/api/v1/books/7/files?isBook=false":
            return self._send([])
        self._send({}, 404)


def main():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    tmp = tempfile.mkdtemp()
    sc = os.path.join(tmp, "Magi.readalong.json")
    json.dump({"format": "koreader-readalong", "version": 1, "audio": {"abs_item_id": "li_1"},
               "cues": [[0, 1, 1, "x", "y", 0, "Hi.", []]]}, open(sc, "w"))
    folder = os.path.join(tmp, "abs", "Author", "Magi")
    os.makedirs(folder)
    os.environ.update(ABS_URL=base, ABS_TOKEN="abstok", GRIMMORY_URL=base, GRIMMORY_USER="mark",
                      GRIMMORY_PASSWORD="pw", CF_ACCESS_CLIENT_ID="cfid", CF_ACCESS_CLIENT_SECRET="cfsec")
    P.publish(sc, abs_folder=folder, scan=True, grimmory_book_id="7")
    n = 0

    def ok(c, msg):
        nonlocal n
        n += 1
        if not c:
            sys.exit("FAIL: " + msg)
    ok(os.path.exists(os.path.join(folder, "Magi.readalong.json")), "copied into ABS item folder")
    ok(not os.path.exists(os.path.join(folder, "Magi.readalong.json.part")), "no .part left")
    ok(("POST", "/api/items/li_1/scan", "Bearer abstok", "cfid") in calls, "ABS scan with item id from sidecar + CF headers")
    ok(state["uploaded"] is not None, "Grimmory supplementary upload")
    ok(P.abs_has_sidecar("li_1") == ["Magi.readalong.json"], "ABS lists the sidecar after scan")
    try:
        P.check_sidecar(__file__)
        ok(False, "rejects non-json")
    except (ValueError, SystemExit):
        ok(True, "rejects non-sidecar")
    srv.shutdown()
    print("ok %d checks" % n)


if __name__ == "__main__":
    main()
