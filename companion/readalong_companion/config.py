"""Configuration from environment variables (see env.example)."""
import os
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_sidecar_dir():
    for cand in (os.environ.get("SIDECAR_DIR"),
                 os.path.join(os.path.dirname(os.path.dirname(HERE)), "sidecar"),  # repo checkout
                 "/app/sidecar"):                                                   # Docker image
        if cand and os.path.isfile(os.path.join(cand, "make_readalong_sidecar.py")):
            return cand
    return "/app/sidecar"


def _bool(v, default=False):
    if v is None or v == "":
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def parse_path_map(s) -> List[Tuple[str, str]]:
    """"/audiobooks=/library,/books=/library2" -> [(server_prefix, local_prefix)]
    Longest server prefix wins."""
    out = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        a, b = part.split("=", 1)
        out.append((a.rstrip("/") or "/", b.rstrip("/") or "/"))
    out.sort(key=lambda p: len(p[0]), reverse=True)
    return out


def map_path(path_map, server_path) -> Optional[str]:
    """Translate a path on the ABS server to where it is mounted here."""
    if not server_path:
        return None
    sp = server_path.replace("\\", "/")
    for a, b in path_map:
        if sp == a or sp.startswith(a.rstrip("/") + "/"):
            rest = sp[len(a):].lstrip("/")
            return os.path.join(b, rest) if rest else b
    return None


@dataclass
class Config:
    # Audiobookshelf
    abs_url: str = ""
    abs_token: str = field(default="", repr=False)
    cf_access_client_id: str = field(default="", repr=False)
    cf_access_client_secret: str = field(default="", repr=False)
    library_ids: List[str] = field(default_factory=list)
    path_map: List[Tuple[str, str]] = field(default_factory=list)
    abs_scan_after_publish: bool = False
    page_size: int = 100
    # service
    data_dir: str = "/data"
    host: str = "0.0.0.0"
    port: int = 8765
    admin_token: str = field(default="", repr=False)
    worker_token: str = field(default="", repr=False)
    worker_mode: str = "local"          # local | remote | both | off
    local_claim_delay: int = 0          # both: XPS waits this long so the G14 gets first pick
    scan_interval: int = 6 * 3600
    scan_on_start: bool = True
    # queue
    max_attempts: int = 3
    retry_base: int = 600
    lease_seconds: int = 900
    poll_seconds: int = 15
    # generator
    sidecar_dir: str = field(default_factory=_find_sidecar_dir)
    python: str = sys.executable or "python3"
    generator_override: str = ""        # GENERATOR: another script with the same CLI (tests)
    model: str = "small.en"
    device: str = "auto"
    language: str = "en"
    generator_timeout: int = 48 * 3600
    min_match: float = 0.85
    min_xpointer: float = 0.9
    keep_work: bool = False
    # remote worker side
    companion_url: str = ""
    worker_name: str = ""

    @property
    def generator(self):
        return self.generator_override or os.path.join(self.sidecar_dir, "make_readalong_sidecar.py")

    @property
    def validator(self):
        return os.path.join(self.sidecar_dir, "validate_sidecar.py")

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "state.db")

    @property
    def sidecars_dir(self):
        return os.path.join(self.data_dir, "sidecars")

    @property
    def work_dir(self):
        return os.path.join(self.data_dir, "work")

    @property
    def local_worker_enabled(self):
        return self.worker_mode in ("local", "both")

    @property
    def remote_workers_enabled(self):
        return self.worker_mode in ("remote", "both") and bool(self.worker_token)

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        g = lambda k, d="": (e.get(k) or d).strip()
        c = cls()
        c.abs_url = g("ABS_URL").rstrip("/")
        c.abs_token = g("ABS_TOKEN")
        c.cf_access_client_id = g("CF_ACCESS_CLIENT_ID")
        c.cf_access_client_secret = g("CF_ACCESS_CLIENT_SECRET")
        c.library_ids = [x.strip() for x in g("ABS_LIBRARY_IDS").split(",") if x.strip()]
        c.path_map = parse_path_map(g("ABS_PATH_MAP"))
        c.abs_scan_after_publish = _bool(e.get("ABS_SCAN_AFTER_PUBLISH"))
        c.page_size = int(g("ABS_PAGE_SIZE", "100"))
        c.data_dir = g("DATA_DIR", c.data_dir)
        c.host = g("BIND_HOST", c.host)
        c.port = int(g("PORT", str(c.port)))
        c.admin_token = g("ADMIN_TOKEN")
        c.worker_token = g("WORKER_TOKEN")
        c.worker_mode = g("WORKER_MODE", c.worker_mode).lower()
        if c.worker_mode not in ("local", "remote", "both", "off"):
            raise ValueError("WORKER_MODE must be local, remote, both or off")
        c.local_claim_delay = int(g("LOCAL_CLAIM_DELAY", "0"))
        c.scan_interval = int(g("SCAN_INTERVAL", str(c.scan_interval)))
        c.scan_on_start = _bool(e.get("SCAN_ON_START"), True)
        c.max_attempts = int(g("MAX_ATTEMPTS", str(c.max_attempts)))
        c.retry_base = int(g("RETRY_BASE_SECONDS", str(c.retry_base)))
        c.lease_seconds = int(g("LEASE_SECONDS", str(c.lease_seconds)))
        c.poll_seconds = int(g("POLL_SECONDS", str(c.poll_seconds)))
        if g("SIDECAR_DIR"):
            c.sidecar_dir = g("SIDECAR_DIR")
        c.python = g("GENERATOR_PYTHON", c.python)
        c.generator_override = g("GENERATOR")
        c.model = g("WHISPER_MODEL", c.model)
        c.device = g("WHISPER_DEVICE", c.device)
        c.language = g("WHISPER_LANGUAGE", c.language)
        c.generator_timeout = int(g("GENERATOR_TIMEOUT", str(c.generator_timeout)))
        c.min_match = float(g("MIN_MATCH", str(c.min_match)))
        c.min_xpointer = float(g("MIN_XPOINTER", str(c.min_xpointer)))
        c.keep_work = _bool(e.get("KEEP_WORK"))
        c.companion_url = g("COMPANION_URL").rstrip("/")
        c.worker_name = g("WORKER_NAME") or os.environ.get("HOSTNAME", "") or "worker"
        return c
