import socket
import threading
import time
import urllib.request

import pytest

from baitd import __main__ as app_main


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
