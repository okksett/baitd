"""Entry point: `python -m baitd` / the `baitd` command."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import socket
import threading
import time
import webbrowser

from waitress import create_server

from . import config, instance
from .jobqueue import JobQueue
from .web.server import create_app

HOST = "127.0.0.1"
log = logging.getLogger("baitd")
_server = None
_server_lock = threading.Lock()
_OTHER_WAIT = 5.0  # seconds a second start waits for the first instance to answer
_STOP_SIGNALS = ("SIGTERM", "SIGBREAK", "SIGHUP")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def build_server(port: int, queue: JobQueue):
    """A threaded WSGI server bound to 127.0.0.1 only; already listening when it returns."""
    return create_server(create_app(queue, shutdown=request_shutdown), host=HOST, port=port, ident="Baitd")


def request_shutdown() -> None:
    """Stop the running server (used by tests and signal handlers)."""
    with _server_lock:
        server = _server
    if server is not None:
        server.close()


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _install_stop_signals() -> list[str]:
    """SIGTERM, SIGBREAK (Windows: the console window's ✕ / CTRL_CLOSE_EVENT) and SIGHUP (POSIX:
    the terminal closed) all take the Ctrl+C path, so the queue stops, FFmpeg is killed and the
    partial output is removed. Main thread only."""
    installed = []
    for name in _STOP_SIGNALS:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_interrupt)
            installed.append(name)
    return installed


def _point_at_running_instance(lock_path, no_browser: bool) -> None:
    """Another Baitd holds the lock: send the user there, or explain why we can't."""
    deadline = time.monotonic() + _OTHER_WAIT
    while True:
        url = instance.read_url(lock_path)  # "" while the first one is still starting
        if url and instance.answers(url):
            break
        if time.monotonic() >= deadline:
            raise SystemExit(
                "Baitd ya está abierto en otra ventana, pero no responde → Cierra esa ventana "
                "(o el proceso Baitd en el administrador de tareas) y vuelve a abrir la app")
        time.sleep(0.25)
    print(f"Baitd ya está abierto en {url}", flush=True)
    if not no_browser:
        webbrowser.open(url)


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


def _selftest() -> int:
    from . import selftest  # imported lazily: the normal start does not need it
    try:
        seconds = selftest.run_selftest()
    except selftest.SelftestError as e:
        print(f"SELFTEST FAILED: {e}", flush=True)
        return 1
    print(f"SELFTEST OK {seconds:.1f}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="baitd", description="Baitd: de un tema a un video corto.")
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador, solo mostrar la URL")
    parser.add_argument("--port", type=int, default=None, help="puerto (por defecto, uno libre)")
    parser.add_argument("--selftest", action="store_true",
                        help="comprobar la instalación: genera un vídeo de prueba sin conexión y sale")
    args = parser.parse_args(argv)
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("--port debe estar entre 1024 y 65535")

    if args.selftest:
        raise SystemExit(_selftest())

    # one instance per data folder: a second worker would rerun the first one's running job
    lock = instance.InstanceLock(config.data_dir() / instance.LOCK_NAME)
    if not lock.acquire():
        _point_at_running_instance(lock.path, args.no_browser)
        return
    try:
        _serve(args, lock)
    finally:
        lock.release()


def _serve(args: argparse.Namespace, lock: instance.InstanceLock) -> None:
    handlers = _setup_logging()
    queue = JobQueue(config.data_dir() / "queue.json")
    global _server
    try:
        port = args.port or find_free_port()
        try:
            server = build_server(port, queue)
        except OSError as e:
            log.error("No se pudo abrir el puerto %s: %s", port, e)
            raise SystemExit(
                f"No se pudo abrir el puerto {port} ({e.strerror or e}) → Cierra la otra ventana de "
                "Baitd o inicia con --port <otro número>") from e
        with _server_lock:
            _server = server
        url = f"http://{HOST}:{server.effective_port}/"
        lock.write_url(url)
        if threading.current_thread() is threading.main_thread():
            _install_stop_signals()  # same clean path as Ctrl+C (KeyboardInterrupt in run())
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
