"""python -m readalong_companion serve|worker|scan"""
import argparse
import json
import logging
import signal
import sys
import threading

from .config import Config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="readalong_companion", description=__doc__)
    ap.add_argument("command", choices=["serve", "worker", "scan"], nargs="?", default="serve",
                    help="serve: web UI + scanner + (local) worker; worker: remote GPU worker; scan: one scan, print summary")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if a.command == "worker":
        from .worker import run_forever
        if cfg.data_dir == "/data" and not sys.platform.startswith("linux"):
            cfg.data_dir = "readalong-worker-data"
        run_forever(cfg)
        return
    from .app import App
    app = App(cfg)
    if a.command == "scan":
        print(json.dumps(app.rescan(), indent=1))
        print(json.dumps(app.store.counts(), indent=1))
        return
    from .web import make_server
    srv = make_server(app)
    signal.signal(signal.SIGTERM, lambda *_: (app.shutdown(), threading.Thread(target=srv.shutdown).start()))
    app.start()
    logging.info("companion on http://%s:%d (mode %s, model %s)", cfg.host, cfg.port, cfg.worker_mode, cfg.model)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
