"""Baitd's own window (pywebview), tested with a fake `webview` module: no real window opens."""
import sys
import threading
import time
import types
import urllib.request

import pytest

from baitd import __main__ as app_main
from baitd import desktop
from baitd.jobqueue import JobQueue
from baitd.web import server
from test_server import LocalClient

URL = "http://127.0.0.1:1/"


def _fake_webview(monkeypatch, on_start=None, fail=None):
    """Install a fake pywebview. `on_start` runs while the 'window' is open; `fail` is raised by start()."""
    calls = {}

    class Window:
        def destroy(self):
            calls["destroyed"] = True

        def restore(self):
            calls["restored"] = True

        def show(self):
            pass

    mod = types.ModuleType("webview")
    mod.settings = {}

    def create_window(title, url, **kw):
        calls["window"] = (title, url, kw)
        return Window()

    def start(**kw):
        calls["start"] = kw
        if fail:
            raise fail
        if on_start:
            on_start()

    mod.create_window, mod.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", mod)
    monkeypatch.setattr(desktop, "_has_webview2", lambda: True)
    return calls


# ---- desktop.run_window ----

def test_window_shows_the_page_and_keeps_the_right_click_menu(tmp_path, monkeypatch):
    calls = _fake_webview(monkeypatch)
    assert desktop.run_window(URL, tmp_path / "ventana") is True
    title, url, kw = calls["window"]
    assert (title, url) == ("Baitd", URL) and kw["min_size"] == desktop.MIN_SIZE
    assert calls["start"]["debug"] is True  # pywebview only keeps the right-click menu with debug
    assert sys.modules["webview"].settings["OPEN_DEVTOOLS_IN_DEBUG"] is False
    assert calls["start"]["private_mode"] is False and (tmp_path / "ventana").is_dir()  # remembers the page's state


def test_no_window_possible_means_the_browser(tmp_path, monkeypatch):
    _fake_webview(monkeypatch, fail=RuntimeError("You must have either QT or GTK"))
    assert desktop.run_window(URL, tmp_path) is False
    monkeypatch.setitem(sys.modules, "webview", None)  # pywebview not installed: import fails
    assert desktop.run_window(URL, tmp_path) is False


def test_windows_without_webview2_does_not_get_internet_explorer(tmp_path, monkeypatch):
    calls = _fake_webview(monkeypatch)
    monkeypatch.setattr(desktop, "_has_webview2", lambda: False)
    monkeypatch.setattr(sys, "platform", "win32")
    assert desktop.run_window(URL, tmp_path) is False
    assert "start" not in calls


def test_focus_and_close_reach_the_open_window(tmp_path, monkeypatch):
    seen = {}

    def while_open():
        seen["focus"] = desktop.focus_window()
        desktop.close_window()

    calls = _fake_webview(monkeypatch, on_start=while_open)
    desktop.run_window(URL, tmp_path)
    assert seen["focus"] is True and calls["restored"] and calls["destroyed"]
    assert desktop.focus_window() is False  # closed: nothing to bring forward
    desktop.close_window()  # and nothing to close


# ---- /api/focus ----

def test_focus_route_says_whether_a_window_came_forward(tmp_data):
    q = JobQueue(tmp_data / "queue.json")
    for focus, ok in ((lambda: True, True), (lambda: False, False), (None, False)):
        a = server.create_app(q, focus=focus)
        a.test_client_class = LocalClient
        r = a.test_client().post("/api/focus", json={})
        assert r.status_code == 200 and r.json == {"ok": ok}
    a = server.create_app(q, focus=lambda: pytest.fail("a foreign page must not reach it"))
    a.test_client_class = LocalClient
    assert a.test_client().post("/api/focus", json={}, base_url="http://evil.example:5000").status_code == 403


# ---- __main__: the window in front, the server behind ----

def _run_main(args, timeout=20):
    t = threading.Thread(target=app_main.main, args=(args,), daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "Baitd did not quit"


def test_closing_the_window_quits_baitd(tmp_data, monkeypatch):
    port = app_main.find_free_port()
    seen = {}

    def run_window(url, storage):
        seen["status"] = urllib.request.urlopen(url, timeout=5).status  # the server is already up
        seen["url"], seen["storage"] = url, storage
        return True  # the user closed the window

    monkeypatch.setattr(desktop, "run_window", run_window)
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: pytest.fail("no browser with a window"))
    _run_main(["--port", str(port)])
    assert seen["status"] == 200 and seen["url"] == f"http://127.0.0.1:{port}/"
    assert seen["storage"] == tmp_data / "ventana"
    assert app_main.port_is_free(port)  # the server stopped with the window


def test_salir_closes_the_window(tmp_data, monkeypatch):
    closed = threading.Event()

    def run_window(url, storage):
        app_main.request_shutdown()  # what "Salir de Baitd" (or the updater) does
        assert closed.wait(10), "the stopped server must close the window"
        return True

    monkeypatch.setattr(desktop, "run_window", run_window)
    monkeypatch.setattr(desktop, "close_window", closed.set)
    _run_main(["--port", str(app_main.find_free_port())])


def test_without_a_window_the_browser_opens(tmp_data, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "run_window", lambda url, storage: False)
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: (opened.append(url), app_main.request_shutdown()))
    port = app_main.find_free_port()
    _run_main(["--port", str(port)])
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_browser_flag_skips_the_window(tmp_data, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "run_window", lambda *a: pytest.fail("--browser must not open the window"))
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: (opened.append(url), app_main.request_shutdown()))
    _run_main(["--browser", "--port", str(app_main.find_free_port())])
    assert len(opened) == 1


def test_opening_baitd_again_brings_its_window_forward(tmp_data, monkeypatch, capsys):
    focused = []
    monkeypatch.setattr(desktop, "focus_window", lambda: focused.append(1) or True)
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: pytest.fail("no second window or tab"))
    port = app_main.find_free_port()
    first = threading.Thread(target=app_main.main, args=(["--no-browser", "--port", str(port)],), daemon=True)
    first.start()
    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):
        try:
            urllib.request.urlopen(url, timeout=2)
            break
        except OSError:
            time.sleep(0.1)
    try:
        app_main.main([])  # the second start
        assert focused == [1] and f"Baitd ya está abierto en {url}" in capsys.readouterr().out
    finally:
        app_main.request_shutdown()
        first.join(10)


def test_say_survives_a_closed_output(monkeypatch):
    class Gone:
        def write(self, s):
            raise OSError(22, "Invalid argument")

        def flush(self):
            raise OSError(22, "Invalid argument")

    monkeypatch.setattr(sys, "stdout", Gone())
    app_main._say("SELFTEST OK 1.0")  # PowerShell had already closed the pipe: no crash
    monkeypatch.setattr(sys, "stdout", None)
    app_main._say("hola")  # windowed build started by a double-click
