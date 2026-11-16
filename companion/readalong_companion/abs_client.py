"""Minimal Audiobookshelf API client (stdlib only).

Optional Cloudflare Access service-token headers for when ABS is reached
through its public hostname.  Tokens are never logged."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

UA = "readalong-companion"


class ABSError(Exception):
    pass


def access_headers(cf_id, cf_secret):
    if not cf_id:
        return {}
    return {"CF-Access-Client-Id": cf_id, "CF-Access-Client-Secret": cf_secret or ""}


class ABS:
    def __init__(self, url, token, cf_id="", cf_secret="", timeout=120, page_size=100):
        self.url = (url or "").rstrip("/")
        self._token = token or ""
        self._cf = access_headers(cf_id, cf_secret)
        self.timeout = timeout
        self.page_size = page_size

    @classmethod
    def from_config(cls, cfg):
        return cls(cfg.abs_url, cfg.abs_token, cfg.cf_access_client_id, cfg.cf_access_client_secret,
                   page_size=cfg.page_size)

    @property
    def configured(self):
        return bool(self.url and self._token)

    def _open(self, path, method="GET", data=None):
        if not self.configured:
            raise ABSError("ABS_URL / ABS_TOKEN not set")
        h = {"Authorization": "Bearer " + self._token, "User-Agent": UA, "Accept": "application/json"}
        h.update(self._cf)
        r = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(r, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            hint = ""
            if e.code in (401, 403) and "cloudflareaccess" in (e.headers.get("Location", "") + e.geturl()):
                hint = " (blocked by Cloudflare Access: set CF_ACCESS_CLIENT_ID/SECRET or use the LAN URL)"
            raise ABSError("ABS %s %s -> HTTP %s%s" % (method, path.split("?")[0], e.code, hint)) from None
        except urllib.error.URLError as e:
            raise ABSError("ABS %s %s -> %s" % (method, path.split("?")[0], e.reason)) from None

    def get_json(self, path, method="GET", data=None):
        with self._open(path, method, data) as r:
            ctype = r.headers.get("Content-Type", "")
            body = r.read()
            final = r.geturl()
        if "text/html" in ctype or "cloudflareaccess.com" in final:
            raise ABSError("ABS returned HTML for %s: probably a Cloudflare Access login page. "
                           "Set CF_ACCESS_CLIENT_ID/SECRET or use the LAN URL." % path.split("?")[0])
        try:
            return json.loads(body or b"{}")
        except ValueError:
            raise ABSError("ABS returned non-JSON for %s" % path.split("?")[0]) from None

    def libraries(self):
        return self.get_json("/api/libraries").get("libraries", [])

    def library_items(self, library_id):
        page = 0
        while True:
            res = self.get_json("/api/libraries/%s/items?limit=%d&page=%d"
                                % (urllib.parse.quote(library_id), self.page_size, page))
            results = res.get("results") or []
            for it in results:
                yield it
            total = res.get("total") or 0
            page += 1
            if not results or page * self.page_size >= total:
                break

    def item(self, item_id):
        return self.get_json("/api/items/%s?expanded=1" % urllib.parse.quote(item_id))

    def open_file(self, item_id, ino):
        return self._open("/api/items/%s/file/%s/download" % (urllib.parse.quote(item_id), urllib.parse.quote(str(ino))))

    def download(self, item_id, ino, dest):
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        with self.open_file(item_id, ino) as r, open(dest + ".part", "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
        os.replace(dest + ".part", dest)
        return dest

    def scan_item(self, item_id):
        """POST /api/items/ID/scan (needs an admin token) so ABS lists the sidecar now."""
        return self.get_json("/api/items/%s/scan" % urllib.parse.quote(item_id), "POST", b"")


def analyse(item):
    """What an ABS library item (full or minified JSON) has.  Returns a dict:
    n_audio, epub (bool), ebook_file, sidecars (list or None if unknown)."""
    media = item.get("media") or {}
    if "audioFiles" in media:
        n_audio = len(media.get("audioFiles") or [])
    else:
        n_audio = media.get("numAudioFiles") or media.get("numTracks") or 0
    eb = media.get("ebookFile")
    if eb:
        md = eb.get("metadata") or {}
        ext = (md.get("ext") or os.path.splitext(md.get("filename") or "")[1]).lower()
        epub, ebook_file = ext == ".epub", md.get("filename")
    else:
        fmt = (media.get("ebookFormat") or media.get("ebookFileFormat") or "").lower()
        epub, ebook_file = fmt == "epub", None
    sidecars = None
    if "libraryFiles" in item:
        sidecars = [lf["metadata"]["filename"] for lf in item.get("libraryFiles") or []
                    if (lf.get("metadata") or {}).get("filename", "").lower().endswith(".readalong.json")]
    md = media.get("metadata") or {}
    return {"n_audio": n_audio, "epub": epub, "ebook_file": ebook_file, "sidecars": sidecars,
            "title": md.get("title") or item.get("id"), "author": md.get("authorName") or "",
            "updated_at": item.get("updatedAt") or item.get("mtimeMs") or 0,
            "missing": bool(item.get("isMissing") or item.get("isInvalid"))}
