"""Entry point: `python -m baitd` / the `baitd` command."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import socket
import threading
import webbrowser

from waitress import create_server

from . import config
from .jobqueue import JobQueue
from .web.server import create_app

HOST = "127.0.0.1"
log = logging.getLogger("baitd")
_server = None
_server_lock = threading.Lock()


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def build_server(port: int, queue: JobQueue):
    """A threaded WSGI server bound to 127.0.0.1 only; already listening when it returns."""
    return create_server(create_app(queue), host=HOST, port=port, ident="Baitd")


def request_shutdown() -> None:
    """Stop the running server (used by tests and signal handlers)."""
    with _server_lock:
        server = _server
    if server is not None:
        server.close()


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _setup_logging() -> list[logging.Handler]:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        config.data_dir() / "baitd.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    handlers = [file_handler, console]
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    return handlers


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="baitd", description="Baitd: de un tema a un video corto.")
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador, solo mostrar la URL")
    parser.add_argument("--port", type=int, default=None, help="puerto (por defecto, uno libre)")
    args = parser.parse_args(argv)

    handlers = _setup_logging()
    queue = JobQueue(config.data_dir() / "queue.json")
    global _server
    try:
        server = build_server(args.port or find_free_port(), queue)
        with _server_lock:
            _server = server
        url = f"http://{HOST}:{server.effective_port}/"
        if threading.current_thread() is threading.main_thread():
            # SIGTERM -> same clean path as Ctrl+C (raises KeyboardInterrupt in run()).
            signal.signal(signal.SIGTERM, _raise_interrupt)
        queue.start()
        log.info("Servidor en %s", url)
        print(url, flush=True)
        print("Pulsa Ctrl+C para cerrar", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        finally:
            server.close()
    finally:
        with _server_lock:
            _server = None
        queue.stop()  # a running job goes back to pending
        for h in handlers:
            logging.getLogger().removeHandler(h)
            h.close()


if __name__ == "__main__":
    main()
