import signal
import socket
import sys
import threading
import time
import urllib.request

import pytest

from baitd import __main__ as app_main
from baitd import instance
from baitd.jobqueue import JobQueue
from baitd.web import server as web_server


def test_find_free_port():
    port = app_main.find_free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as s:  # it is really free right now
        s.bind(("127.0.0.1", port))


def test_main_no_browser_starts(tmp_data, capsys):
    port = app_main.find_free_port()
    t = threading.Thread(target=app_main.main, args=(["--no-browser", "--port", str(port)],), daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    resp = None
    for _ in range(100):
        try:
            resp = urllib.request.urlopen(url, timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    try:
        assert resp is not None and resp.status == 200
    finally:
        app_main.request_shutdown()
        t.join(10)
    assert not t.is_alive()
    out = capsys.readouterr().out
    assert url in out and "Pulsa Ctrl+C para cerrar" in out
    assert (tmp_data / "baitd.log").exists()


def test_port_in_use_exits_with_message(tmp_data):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            app_main.main(["--no-browser", "--port", str(port)])
    assert exc.value.code not in (0, None)
    assert f"puerto {port}" in str(exc.value.code) and "--port" in str(exc.value.code)


def test_port_is_free_detects_reuseaddr_listener():
    # python -m http.server style listener: SO_REUSEADDR lets a plain bind() succeed on Windows,
    # so the probe must not rely on that
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert app_main.port_is_free(port) is False
    assert app_main.port_is_free(app_main.find_free_port()) is True


def test_busy_port_reported_before_server_starts(tmp_data, monkeypatch):
    # a port taken by another program is reported with the Spanish message, and no server is built
    # (the probe runs only on Windows; pin the platform so this passes on the macOS/Linux CI runners)
    monkeypatch.setattr(app_main.sys, "platform", "win32")
    monkeypatch.setattr(app_main, "port_is_free", lambda port: False)
    monkeypatch.setattr(app_main, "build_server",
                        lambda *a, **k: pytest.fail("build_server must not run on a busy port"))
    with pytest.raises(SystemExit) as exc:
        app_main.main(["--no-browser", "--port", "5123"])
    msg = str(exc.value.code)
    assert "No se pudo abrir el puerto 5123 (está en uso)" in msg
    assert "→ Cierra la otra ventana de Baitd o inicia con --port <otro número>" in msg


def test_busy_port_probe_is_skipped_off_windows(tmp_data, monkeypatch):
    # POSIX: waitress binds with SO_REUSEADDR and reports a busy port itself; the probe would fail
    # on ports still in TIME_WAIT
    monkeypatch.setattr(app_main.sys, "platform", "linux")
    monkeypatch.setattr(app_main, "port_is_free", lambda port: pytest.fail("probe must not run"))

    class Reached(Exception):
        pass

    def build(port, queue):
        raise Reached

    monkeypatch.setattr(app_main, "build_server", build)
    with pytest.raises(Reached):
        app_main.main(["--no-browser", "--port", "5123"])


def test_port_out_of_range_rejected(tmp_data):
    with pytest.raises(SystemExit) as exc:
        app_main.main(["--no-browser", "--port", "80"])
    assert exc.value.code == 2


# ---- I5: every "the window is closing" signal takes the graceful path ----

def test_stop_signals_raise_keyboard_interrupt():
    names = [n for n in ("SIGTERM", "SIGBREAK", "SIGHUP") if hasattr(signal, n)]
    saved = {n: signal.getsignal(getattr(signal, n)) for n in names}
    try:
        installed = app_main._install_stop_signals()
        assert installed == names
        assert ("SIGBREAK" in names) == (sys.platform == "win32")  # console ✕ -> CTRL_CLOSE_EVENT
        for n in names:
            assert signal.getsignal(getattr(signal, n)) is app_main._raise_interrupt
        with pytest.raises(KeyboardInterrupt):
            app_main._raise_interrupt(signal.SIGTERM, None)
    finally:
        for n, h in saved.items():
            signal.signal(getattr(signal, n), h)


# ---- I6: single instance ----

def _start_in_thread(port):
    t = threading.Thread(target=app_main.main, args=(["--no-browser", "--port", str(port)],), daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=2)
            return t, url
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the first instance did not start")


def test_instance_lock_is_exclusive(tmp_path):
    a, b = instance.InstanceLock(tmp_path / "x.lock"), instance.InstanceLock(tmp_path / "x.lock")
    assert a.acquire()
    try:
        assert not b.acquire()
        a.write_url("http://127.0.0.1:5000/")
        assert instance.read_url(tmp_path / "x.lock") == "http://127.0.0.1:5000/"
    finally:
        a.release()
    assert b.acquire()
    b.release()


@pytest.mark.parametrize("no_browser", [True, False])
def test_second_start_points_at_the_first(tmp_data, capsys, monkeypatch, no_browser):
    opened = []
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: opened.append(url))
    t, url = _start_in_thread(app_main.find_free_port())
    try:
        capsys.readouterr()
        app_main.main(["--no-browser"] if no_browser else [])  # returns: exit code 0
        out = capsys.readouterr().out
        assert f"Baitd ya está abierto en {url}" in out
        assert opened == ([] if no_browser else [url])
    finally:
        app_main.request_shutdown()
        t.join(10)
    assert not t.is_alive()


def test_held_lock_that_does_not_answer_exits_1(tmp_data, monkeypatch):
    monkeypatch.setattr(app_main, "_OTHER_WAIT", 0.3)
    held = instance.InstanceLock(tmp_data / instance.LOCK_NAME)
    assert held.acquire()
    try:
        held.write_url(f"http://127.0.0.1:{app_main.find_free_port()}/")  # nothing listens there
        with pytest.raises(SystemExit) as exc:
            app_main.main(["--no-browser"])
    finally:
        held.release()
    msg = str(exc.value.code)
    assert exc.value.code not in (0, None) and "no responde" in msg and "→" in msg
    assert not (tmp_data / "queue.json").exists()  # never touched the other instance's queue


def test_selftest_does_not_need_the_lock(tmp_data, monkeypatch, capsys):
    from baitd import selftest
    monkeypatch.setattr(selftest, "run_selftest", lambda: 1.0)
    held = instance.InstanceLock(tmp_data / instance.LOCK_NAME)
    assert held.acquire()
    try:
        with pytest.raises(SystemExit) as exc:
            app_main.main(["--selftest"])
    finally:
        held.release()
    assert exc.value.code == 0 and "SELFTEST OK" in capsys.readouterr().out


def test_shutdown_exits_even_with_an_open_browser_connection(tmp_data):
    # The browser keeps an idle keep-alive connection: closing only the listening socket left
    # run() looping forever (and, with I6, the instance lock held by a process that no longer
    # answers). request_shutdown must close every connection so main() returns.
    import http.client
    t, url = _start_in_thread(app_main.find_free_port())
    conn = http.client.HTTPConnection("127.0.0.1", int(url.rsplit(":", 1)[1].strip("/")), timeout=5)
    try:
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200  # the connection stays open (HTTP/1.1 keep-alive)
        app_main.request_shutdown()
        t.join(10)
        assert not t.is_alive()
    finally:
        conn.close()
        if t.is_alive():
            app_main.request_shutdown()


def test_request_shutdown_falls_back_when_waitress_internals_missing(monkeypatch):
    """R33 guard: without waitress's trigger/_map, shutdown still closes the server."""
    class FakeServer:
        closed = False

        def close(self):
            self.closed = True

    fake = FakeServer()
    monkeypatch.setattr(app_main, "_server", fake)
    app_main.request_shutdown()
    assert fake.closed
    fake.closed = False
    app_main._close_everything(fake)  # no _map attribute: must not raise
    assert fake.closed


def test_unwritable_data_dir_exits_with_message(monkeypatch, capsys):
    def boom(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(app_main.instance.InstanceLock, "acquire", boom)
    import pytest as _pytest
    with _pytest.raises(SystemExit) as exc:
        app_main.main(["--no-browser"])
    assert exc.value.code == 1
    assert "→" in capsys.readouterr().out


def test_start_sweeps_orphan_clip_data(tmp_data):
    orphan = tmp_data / "fuentes" / ("0" * 12)
    orphan.mkdir(parents=True)
    (orphan / "source.mp4").write_bytes(b"half an upload")
    analysis = tmp_data / "analisis" / ("1" * 12 + ".json")
    analysis.parent.mkdir()
    analysis.write_text('{"source_id": "000000000000"}', encoding="utf-8")
    port = app_main.find_free_port()
    t = threading.Thread(target=app_main.main, args=(["--no-browser", "--port", str(port)],), daemon=True)
    t.start()
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
                break
            except OSError:
                time.sleep(0.1)
        assert not orphan.exists() and not analysis.exists()
    finally:
        app_main.request_shutdown()
        t.join(10)


def test_request_body_cap_fits_one_upload_chunk(tmp_data):
    # uploads come in chunks (F3): waitress's cap stays at or under its 1 GB default, above one chunk
    assert web_server.UPLOAD_CHUNK_MAX < app_main.MAX_BODY <= 1024**3
    server = app_main.build_server(app_main.find_free_port(), JobQueue(tmp_data / "queue.json"))
    try:
        assert server.adj.max_request_body_size == app_main.MAX_BODY
    finally:
        server.close()
