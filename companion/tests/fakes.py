"""Fake Audiobookshelf server and fixtures for the companion tests."""
import copy
import http.server
import json
import os
import threading
import urllib.parse
import zipfile

TOKEN = "abs-test-token"
CF_ID, CF_SECRET = "cf-id.access", "cf-secret"

XHTML = ('<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title>'
         '</head><body><p>Hello world. This is a test.</p></body></html>')


def make_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   '</rootfiles></container>')
        z.writestr("OEBPS/content.opf",
                   '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Fixture</dc:title></metadata>'
                   '<manifest><item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
                   '<spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/ch1.xhtml", XHTML)
    return path


def book_item(item_id, title, folder, audio=("01 Intro.mp3", "02 Chapter.mp3"), epub="Fixture Book.epub",
              sidecar=None, updated=1000, ino0=100):
    """An expanded ABS item.  folder is the path on the (fake) ABS server."""
    afs = [{"ino": str(ino0 + i), "index": i + 1, "exclude": False,
            "metadata": {"filename": n, "ext": os.path.splitext(n)[1], "path": folder + "/" + n, "size": 4}}
           for i, n in enumerate(audio)]
    lfs = [{"ino": a["ino"], "metadata": a["metadata"], "fileType": "audio"} for a in afs]
    eb = None
    if epub:
        eb = {"ino": str(ino0 + 50), "ebookFormat": os.path.splitext(epub)[1][1:],
              "metadata": {"filename": epub, "ext": os.path.splitext(epub)[1], "path": folder + "/" + epub, "size": 9}}
        lfs.append({"ino": eb["ino"], "metadata": eb["metadata"], "fileType": "ebook"})
    if sidecar:
        lfs.append({"ino": str(ino0 + 60), "metadata": {"filename": sidecar, "ext": ".json"}, "fileType": "metadata"})
    return {"id": item_id, "path": folder, "updatedAt": updated, "mediaType": "book", "isMissing": False,
            "media": {"metadata": {"title": title, "authorName": "A. Author"}, "audioFiles": afs, "ebookFile": eb},
            "libraryFiles": lfs}


def minified(item):
    m = item["media"]
    return {"id": item["id"], "path": item["path"], "updatedAt": item["updatedAt"], "isMissing": False,
            "media": {"metadata": m["metadata"], "numAudioFiles": len(m["audioFiles"]),
                      "ebookFormat": (m["ebookFile"] or {}).get("ebookFormat")}}


class FakeABS:
    def __init__(self, epub_bytes, require_cf=True):
        self.libraries = [{"id": "lib_books", "name": "Audiobooks", "mediaType": "book"},
                          {"id": "lib_pods", "name": "Podcasts", "mediaType": "podcast"}]
        self.items = {}        # id -> expanded item (in lib_books)
        self.files = {}        # (item_id, ino) -> bytes
        self.calls = []
        self.scans = []
        self.epub_bytes = epub_bytes
        self.require_cf = require_cf
        self.lock = threading.Lock()

    def add(self, item):
        self.items[item["id"]] = item
        for a in item["media"]["audioFiles"]:
            self.files[(item["id"], a["ino"])] = b"AUD" + a["metadata"]["filename"].encode()[:1]
        if item["media"]["ebookFile"]:
            self.files[(item["id"], item["media"]["ebookFile"]["ino"])] = self.epub_bytes

    def start(self):
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, body=None, ctype="application/json"):
                body = body if body is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth(self):
                ok = self.headers.get("Authorization") == "Bearer " + TOKEN
                if fake.require_cf:
                    ok = ok and self.headers.get("CF-Access-Client-Id") == CF_ID \
                        and self.headers.get("CF-Access-Client-Secret") == CF_SECRET
                if not ok:
                    self._send(401, {"error": "unauthorised"})
                return ok

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = urllib.parse.parse_qs(u.query)
                with fake.lock:
                    fake.calls.append(("GET", self.path))
                if not self._auth():
                    return
                p = u.path.strip("/").split("/")
                if p == ["api", "libraries"]:
                    return self._send(200, {"libraries": fake.libraries})
                if len(p) == 4 and p[:2] == ["api", "libraries"] and p[3] == "items":
                    if p[2] != "lib_books":
                        return self._send(200, {"results": [], "total": 0})
                    limit, page = int(q["limit"][0]), int(q["page"][0])
                    allit = [minified(i) for i in fake.items.values()]
                    return self._send(200, {"results": allit[page * limit:(page + 1) * limit], "total": len(allit),
                                            "limit": limit, "page": page})
                if len(p) == 3 and p[:2] == ["api", "items"]:
                    it = fake.items.get(p[2])
                    return self._send(200, copy.deepcopy(it)) if it else self._send(404, {})
                if len(p) == 6 and p[:2] == ["api", "items"] and p[3] == "file" and p[5] == "download":
                    b = fake.files.get((p[2], p[4]))
                    return self._send(200, body=b, ctype="application/octet-stream") if b else self._send(404, {})
                self._send(404, {})

            def do_POST(self):
                with fake.lock:
                    fake.calls.append(("POST", self.path))
                if not self._auth():
                    return
                p = self.path.strip("/").split("/")
                if len(p) == 4 and p[:2] == ["api", "items"] and p[3] == "scan":
                    fake.scans.append(p[2])
                    return self._send(200, {"result": "UPDATED"})
                self._send(404, {})

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        return self

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()
