"""Entry point: `python -m baitd` / the `baitd` command."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import socket
import sys
import threading
import time
import webbrowser

from waitress import create_server

from . import __version__, config, desktop, instance, updater
from .jobqueue import JobQueue
from .web.server import UPLOAD_CHUNK_MAX, create_app

HOST = "127.0.0.1"
# waitress rejects bigger bodies before Flask sees them: one upload chunk plus headroom, under its 1 GB default
MAX_BODY = UPLOAD_CHUNK_MAX + 1024**2
log = logging.getLogger("baitd")
_server = None
_server_lock = threading.Lock()
_OTHER_WAIT = 5.0  # seconds a second start waits for the first instance to answer
_STOP_SIGNALS = ("SIGTERM", "SIGBREAK", "SIGHUP")


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def port_is_free(port: int) -> bool:
    """False when another program already holds the port. waitress sets SO_REUSEADDR, which on
    Windows lets it bind a port that another SO_REUSEADDR socket already listens on, so the probe
    takes the port exclusively there (SO_EXCLUSIVEADDRUSE) and uses a plain bind elsewhere."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if sys.platform == "win32" and exclusive is not None:
            s.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def build_server(port: int, queue: JobQueue):
    """A threaded WSGI server bound to 127.0.0.1 only; already listening when it returns."""
    return create_server(create_app(queue, shutdown=request_shutdown, focus=desktop.focus_window),
                         host=HOST, port=port, ident="Baitd",
                         max_request_body_size=MAX_BODY)  # uploads come in chunks; the route checks too


def _close_everything(server) -> None:
    """Runs in the server's own loop thread: close every open connection, then the server.

    Closing only the listening socket is not enough: the browser's idle keep-alive connection
    stays in waitress's map and run() keeps looping, so the process (and its instance lock)
    would outlive "Salir"."""
    try:
        conn_map = getattr(server, "_map", None) or {}  # waitress keeps its dispatchers in _map
        trigger = getattr(server, "trigger", None)
        for obj in list(conn_map.values()):
            if obj is not server and obj is not trigger:
                try:
                    obj.close()
                except Exception:  # one bad connection must not keep the process alive
                    log.debug("closing a connection failed", exc_info=True)
    finally:
        server.close()


def request_shutdown() -> None:
    """Stop the running server (used by the Salir endpoint and tests); run() then returns."""
    with _server_lock:
        server = _server
    if server is None:
        return
    trigger = getattr(server, "trigger", None)
    try:
        if trigger is None:
            raise AttributeError("waitress server has no trigger")
        trigger.pull_trigger(lambda: _close_everything(server))  # thread-safe hand-off
    except (OSError, AttributeError):  # trigger closed or internals changed: plain close
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
    _say(f"Baitd ya está abierto en {url}")
    if not no_browser and not _ask_to_focus(url):
        webbrowser.open(url)


def _ask_to_focus(url: str) -> bool:
    """Ask the running Baitd to bring its window to the front. False when it has no window (it runs
    in the browser) or does not answer, so the caller opens the browser instead."""
    import json
    import urllib.request
    req = urllib.request.Request(url + "api/focus", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except (OSError, ValueError):
        return False


def _setup_logging() -> list[logging.Handler]:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(
        config.data_dir() / "baitd.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handlers: list[logging.Handler] = [file_handler]
    if sys.stderr is not None:  # the windowed build started by a double-click has no console
        console = logging.StreamHandler()
        console.setLevel(logging.WARNING)
        handlers.append(console)
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
        _say(f"SELFTEST FAILED: {e}")
        return 1
    _say(f"SELFTEST OK {seconds:.1f}")
    return 0


def _say(text: str) -> None:
    """print() that never takes Baitd down: the windowed build may have nowhere to write, and a pipe
    can be gone by the time we write (PowerShell does not wait for a windowed program)."""
    try:
        print(text, flush=True)
    except (OSError, ValueError, AttributeError):
        pass


def _reattach_stdio() -> None:
    """The Windows build has no console (Baitd has its own window), so print() would go nowhere. When
    a pipe or a terminal started it (CI's --selftest, someone running Baitd.exe from cmd), write there."""
    if sys.platform != "win32" or (sys.stdout is not None and sys.stderr is not None):
        return
    import ctypes
    import msvcrt
    import os
    kernel32 = ctypes.windll.kernel32
    attached = False
    for std, name in ((-11, "stdout"), (-12, "stderr")):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
        if getattr(sys, name) is not None:
            continue
        handle = kernel32.GetStdHandle(std)
        try:
            if handle not in (0, -1, None):  # an inherited pipe or file
                stream = open(msvcrt.open_osfhandle(handle, os.O_WRONLY), "w", encoding="utf-8",
                              errors="replace", buffering=1)
            elif attached or kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS: started from cmd
                attached = True
                stream = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
            else:  # double-clicked: nobody to write to
                continue
        except OSError:
            continue
        setattr(sys, name, stream)


def main(argv: list[str] | None = None) -> None:
    _reattach_stdio()
    parser = argparse.ArgumentParser(prog="baitd", description="Baitd: de un tema a un video corto, y clips de un video largo.")
    parser.add_argument("--no-browser", action="store_true", help="no abrir ventana ni navegador, solo mostrar la URL")
    parser.add_argument("--browser", action="store_true", help="abrir Baitd en el navegador en vez de en su ventana")
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
    try:
        acquired = lock.acquire()
    except OSError as e:  # data folder missing/read-only: explain instead of a traceback
        _say(f"No se puede usar la carpeta de datos {lock.path.parent} ({e.strerror or e}) → "
             "Revisa que exista y que tengas permiso de escritura.")
        raise SystemExit(1)
    if not acquired:
        _point_at_running_instance(lock.path, args.no_browser)
        return
    try:
        _serve(args, lock)
    finally:
        lock.release()


def _serve(args: argparse.Namespace, lock: instance.InstanceLock) -> None:
    handlers = _setup_logging()
    try:  # 0.3: the AI provider/model moved from the profiles to the settings
        config.migrate_llm_settings()
    except OSError:
        log.exception("No se pudo migrar el proveedor de IA a los ajustes")
    queue = JobQueue(config.data_dir() / "queue.json")
    try:  # before the server can accept an upload: nothing in flight can be mistaken for a leftover
        queue.sweep_clip_data(keep_sources=bool(config.load_settings().get("keep_sources")))
    except OSError:
        log.exception("No se pudo limpiar fuentes/ y analisis/")
    global _server
    try:
        # Windows only: waitress would share the port silently there. Elsewhere the probe fails on ports in
        # TIME_WAIT, while waitress binds fine and its OSError below already reports a busy port.
        if sys.platform == "win32" and args.port is not None and not port_is_free(args.port):
            log.error("No se pudo abrir el puerto %s: está en uso", args.port)
            raise SystemExit(
                f"No se pudo abrir el puerto {args.port} (está en uso) → Cierra la otra ventana de "
                "Baitd o inicia con --port <otro número>")
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
        _say(url)
        _say("Pulsa Ctrl+C para cerrar")
        if config.load_settings().get("check_updates", True):
            updater.start_background_check(__version__)
        if args.no_browser:
            _run_server(server)
        elif getattr(args, "browser", False):
            webbrowser.open(url)
            _run_server(server)
        else:
            _run_with_window(server, url)
    finally:
        with _server_lock:
            _server = None
        queue.stop()  # a running job goes back to pending
        for h in handlers:
            logging.getLogger().removeHandler(h)
            h.close()


def _run_server(server) -> None:
    """Serve until Salir, the updater or Ctrl+C (and the console's ✕, which arrive as KeyboardInterrupt)."""
    try:
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


def _run_with_window(server, url: str) -> None:
    """Baitd's own window in front (main thread), the server behind it (a thread). Closing the window
    quits; Salir or the updater stop the server, which then closes the window. With no window
    possible here, the browser opens and the server runs as before."""
    stopped = threading.Event()

    def serve() -> None:
        try:
            server.run()
        except Exception:  # the window must still close and the process end
            log.exception("El servidor se detuvo por un error")
        finally:
            server.close()
            stopped.set()
            desktop.close_window()

    worker = threading.Thread(target=serve, name="baitd-server", daemon=True)
    worker.start()
    try:
        shown = desktop.run_window(url, config.data_dir() / "ventana")
    except KeyboardInterrupt:
        shown = True
    if shown:
        request_shutdown()  # the window closed (or Salir closed it): stop the server too
    else:
        webbrowser.open(url)
    try:
        while not stopped.wait(0.5):  # short waits so Ctrl+C still gets through
            pass
    except KeyboardInterrupt:
        request_shutdown()
        stopped.wait(10)


if __name__ == "__main__":
    main()
