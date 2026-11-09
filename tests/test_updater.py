import hashlib

import pytest
import requests

from baitd import __main__ as entry
from baitd import updater
from baitd.models import UserError

PFX = updater.DOWNLOAD_PREFIX
SETUP = PFX + "v0.4.0/Baitd-Setup.exe"
SUMS = PFX + "v0.4.0/SHA256SUMS"


class Resp:
    def __init__(self, payload=None, text="", body=b"", status=200):
        self.payload, self.text, self.body, self.status = payload, text, body, status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(str(self.status))

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]


def release(tag="v0.4.0", setup=SETUP, sums=SUMS):
    return {"tag_name": tag, "html_url": f"https://github.com/okksett/baitd/releases/tag/{tag}",
            "assets": [{"name": "Baitd-Setup.exe", "browser_download_url": setup},
                       {"name": "SHA256SUMS", "browser_download_url": sums}]}


def test_parse_version():
    assert updater.parse_version("v0.3.1") == (0, 3, 1)
    assert updater.parse_version("1.10") == (1, 10)
    assert updater.parse_version("nightly") == ()
    assert updater.parse_version("0.3.0-beta") == ()
    assert updater.parse_version("") == ()


def test_check_newer_sends_headers_and_timeout():
    seen = {}

    def get(url, **kw):
        seen.update(url=url, **kw)
        return Resp(release())

    latest = updater.check("0.3.0", get=get)
    assert latest == {"version": "0.4.0", "page": "https://github.com/okksett/baitd/releases/tag/v0.4.0",
                      "setup_url": SETUP, "sums_url": SUMS}
    assert seen["url"] == updater.LATEST_API and seen["timeout"] == 5
    assert seen["headers"] == {"Accept": "application/vnd.github+json", "User-Agent": "Baitd/0.3.0"}


@pytest.mark.parametrize("tag", ["v0.3.0", "0.3.0", "v0.2.9", "v0.3", "garbage"])
def test_check_equal_or_older_is_none(tag):
    assert updater.check("0.3.0", get=lambda *a, **k: Resp(release(tag))) is None


def test_check_failures_are_none():
    assert updater.check("0.3.0", get=lambda *a, **k: Resp(ValueError("bad json"))) is None
    assert updater.check("0.3.0", get=lambda *a, **k: Resp({"nope": 1})) is None

    def boom(*a, **k):
        raise requests.ConnectionError("offline")

    assert updater.check("0.3.0", get=boom) is None
    assert updater.check("0.3.0", get=lambda *a, **k: Resp(status=403)) is None


def test_check_drops_foreign_asset_urls_and_page():
    data = release(setup="https://evil.example/Baitd-Setup.exe", sums=SUMS)
    data["html_url"] = "https://evil.example/x"
    latest = updater.check("0.3.0", get=lambda *a, **k: Resp(data))
    assert latest["setup_url"] == "" and latest["sums_url"] == SUMS
    assert latest["page"].startswith("https://github.com/okksett/baitd/")


def test_can_install(monkeypatch):
    latest = {"setup_url": SETUP, "sums_url": SUMS}
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    assert updater.can_install(latest) is False  # not frozen
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    assert updater.can_install(latest) is True
    assert updater.can_install({"setup_url": "", "sums_url": SUMS}) is False
    assert updater.can_install(None) is False
    monkeypatch.setattr(updater.sys, "platform", "linux")
    assert updater.can_install(latest) is False


INSTALLER = b"MZ-fake-installer" * 10000
LATEST = {"version": "0.4.0", "setup_url": SETUP, "sums_url": SUMS}
ZEROS = "0" * 64


def fake_get(sums_text, body=INSTALLER, calls=None):
    def get(url, **kw):
        if calls is not None:
            calls.append(url)
        return Resp(text=sums_text) if url == SUMS else Resp(body=body)
    return get


def test_download_good_hash(tmp_path):
    good = hashlib.sha256(INSTALLER).hexdigest()
    sums = f"{ZEROS}  other.zip\n{good}  Baitd-Setup.exe\n"
    path = updater.download_and_verify(LATEST, tmp_path, get=fake_get(sums))
    assert path == tmp_path / "Baitd-Setup-0.4.0.exe" and path.read_bytes() == INSTALLER
    assert [p.name for p in tmp_path.iterdir()] == ["Baitd-Setup-0.4.0.exe"]


def test_download_mismatch_or_missing_line_deletes_and_raises(tmp_path):
    for sums in (f"{ZEROS}  Baitd-Setup.exe\n", f"{'a' * 64}  other.zip\n"):
        with pytest.raises(UserError) as e:
            updater.download_and_verify(LATEST, tmp_path, get=fake_get(sums))
        assert "No se pudo verificar la actualización" in str(e.value)
        assert list(tmp_path.iterdir()) == []


def test_download_network_error_is_user_error(tmp_path):
    def boom(url, **kw):
        raise requests.ConnectionError("x")

    with pytest.raises(UserError) as e:
        updater.download_and_verify(LATEST, tmp_path, get=boom)
    assert "No se pudo descargar la actualización" in str(e.value)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_foreign_urls_without_fetching(tmp_path):
    calls = []
    for bad in ({"version": "0.4.0", "setup_url": "https://evil.example/s.exe", "sums_url": SUMS},
                {"version": "0.4.0", "setup_url": SETUP, "sums_url": "https://evil.example/SHA256SUMS"}):
        with pytest.raises(UserError):
            updater.download_and_verify(bad, tmp_path, get=fake_get("", calls=calls))
    assert calls == []


def test_download_clears_old_installers(tmp_path):
    (tmp_path / "Baitd-Setup-0.3.5.exe").write_bytes(b"old")
    good = hashlib.sha256(INSTALLER).hexdigest()
    updater.download_and_verify(LATEST, tmp_path, get=fake_get(f"{good}  Baitd-Setup.exe"))
    assert [p.name for p in tmp_path.iterdir()] == ["Baitd-Setup-0.4.0.exe"]


def test_launch_installer_flags(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(updater.subprocess, "Popen", lambda args, **kw: seen.update(args=args, kw=kw))
    monkeypatch.setattr(updater.sys, "platform", "win32")
    monkeypatch.setattr(updater.subprocess, "DETACHED_PROCESS", 8, raising=False)
    monkeypatch.setattr(updater.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False)
    updater.launch_installer(tmp_path / "s.exe")
    assert seen["args"][1:] == ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/UPDATE=1"]
    assert seen["kw"]["close_fds"] is True and seen["kw"]["creationflags"] == 520


def test_background_check_fills_state(monkeypatch):
    monkeypatch.setattr(updater, "state", {"latest": None})
    monkeypatch.setattr(updater, "check", lambda current: {"version": "9.9.9"})
    updater.start_background_check("0.3.0").join(2)
    assert updater.state["latest"] == {"version": "9.9.9"}


def _serve(monkeypatch, check_updates):
    from baitd import config
    config.save_settings({"check_updates": check_updates})
    started = []
    monkeypatch.setattr(entry.updater, "start_background_check", lambda cur: started.append(cur))

    class FakeServer:
        effective_port = 1

        def run(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(entry, "build_server", lambda port, queue: FakeServer())
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: None)
    args = entry.argparse.Namespace(no_browser=True, port=None)
    lock = type("L", (), {"write_url": lambda self, url: None})()
    entry._serve(args, lock)
    return started


def test_serve_starts_check_only_when_enabled(monkeypatch, tmp_data):
    from baitd import __version__
    assert _serve(monkeypatch, True) == [__version__]
    assert _serve(monkeypatch, False) == []


@pytest.mark.parametrize("url", [
    PFX + "../../../../attacker/repo/releases/download/v1/Baitd-Setup.exe",
    PFX + "v1/../../../../attacker/repo/releases/download/v1/Baitd-Setup.exe",
    PFX + "%2e%2e/%2e%2e/x/Baitd-Setup.exe",
    PFX + "../Baitd-Setup.exe",
    "https://evil.example/okksett/baitd/releases/download/v1/Baitd-Setup.exe",
    "https://github.com/attacker/baitd/releases/download/v1/Baitd-Setup.exe",
    PFX + "v1/extra/Baitd-Setup.exe",
    PFX + "v1/Other.exe",
    PFX + "v1/Baitd-Setup.exe?x=1",
    PFX + "v1/Baitd-Setup.exe#frag",
    "http://github.com/okksett/baitd/releases/download/v1/Baitd-Setup.exe",
    "https://user@github.com/okksett/baitd/releases/download/v1/Baitd-Setup.exe",
    "https://github.com:444/okksett/baitd/releases/download/v1/Baitd-Setup.exe",
    "https://github.com.evil.example/okksett/baitd/releases/download/v1/Baitd-Setup.exe",
    "", None,
])
def test_asset_url_rejected(url):
    assert updater.is_release_asset_url(url) is False


def test_asset_url_valid():
    assert updater.is_release_asset_url(PFX + "v0.4.0/Baitd-Setup.exe")
    assert updater.is_release_asset_url(PFX + "v0.4.0/SHA256SUMS")


def test_dot_segment_url_is_dropped_by_check_and_refused_by_download(tmp_path):
    evil = PFX + "../../../../attacker/repo/releases/download/v1/Baitd-Setup.exe"
    latest = updater.check("0.3.0", get=lambda *a, **k: Resp(release(setup=evil)))
    assert latest["setup_url"] == ""
    calls = []
    with pytest.raises(UserError):
        updater.download_and_verify({"version": "0.4.0", "setup_url": evil, "sums_url": SUMS}, tmp_path,
                                    get=fake_get("", calls=calls))
    assert calls == []


def test_part_altered_on_disk_is_rejected_and_deleted(tmp_path, monkeypatch):
    good = hashlib.sha256(INSTALLER).hexdigest()
    real = updater._file_digest

    def tampered(path):
        path.write_bytes(b"evil")  # swapped after the stream was hashed
        return real(path)

    monkeypatch.setattr(updater, "_file_digest", tampered)
    with pytest.raises(UserError):
        updater.download_and_verify(LATEST, tmp_path, get=fake_get(f"{good}  Baitd-Setup.exe"))
    assert list(tmp_path.iterdir()) == []
