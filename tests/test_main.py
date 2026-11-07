import signal
import socket
import sys
import threading
import time
import urllib.request

import pytest

from baitd import __main__ as app_main
from baitd import instance


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
