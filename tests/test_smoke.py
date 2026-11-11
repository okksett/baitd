"""Offline end-to-end smoke test: the same code the frozen app runs with `--selftest`."""
import pytest

from baitd import __main__ as app_main
from baitd import selftest
from baitd.ffmpeg import has_filter, probe


def test_offline_end_to_end(tmp_data, tmp_path_factory):
    assert has_filter("ass")
    work = tmp_path_factory.mktemp("selftest")
    seconds = selftest.run_selftest(work)
    assert seconds > 0
    video = next(work.glob("out/*/video.mp4"))
    info = probe(video)
    assert (info["width"], info["height"]) == (1080, 1920)
    assert abs(info["fps"] - 30) < 0.01
    assert abs(info["duration"] - selftest.VOICE_SECONDS) < 0.3
    assert list(tmp_data.iterdir()) == []  # the user's data folder stays untouched


def test_selftest_restores_data_dir_env(tmp_data):
    import os
    before = os.environ["BAITD_DATA"]
    selftest.run_selftest()
    assert os.environ["BAITD_DATA"] == before


def test_main_selftest_ok(monkeypatch, capsys):
    monkeypatch.setattr(selftest, "run_selftest", lambda *a, **k: 12.3)
    with pytest.raises(SystemExit) as exc:
        app_main.main(["--selftest"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "SELFTEST OK 12.3"


def test_main_selftest_failure_exits_1(monkeypatch, capsys):
    def boom(*a, **k):
        raise selftest.SelftestError("el filtro ass no está disponible")
    monkeypatch.setattr(selftest, "run_selftest", boom)
    with pytest.raises(SystemExit) as exc:
        app_main.main(["--selftest"])
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "SELFTEST FAILED" in out.out and "filtro ass" in out.out


def test_selftest_checks_clip_dependencies(monkeypatch):
    calls = []
    from baitd.clip import framing, ytdlp_loader
    real = framing._make_detector
    monkeypatch.setattr(framing, "_make_detector", lambda size: (calls.append("yunet"), real(size))[1])
    monkeypatch.setattr(ytdlp_loader, "import_ytdlp", lambda: calls.append("ytdlp"))
    selftest._check_clip_deps()
    assert calls == ["yunet", "ytdlp"]


def test_selftest_runs_the_bundled_deno():
    from baitd.clip import ytdlp_loader
    path = ytdlp_loader.deno_path()
    assert path, "the deno package is a dependency, so a Deno must be found"
    selftest._check_deno(path)  # runs `deno --version` and imports yt-dlp's solver scripts


def test_selftest_fails_without_deno(tmp_path):
    with pytest.raises(selftest.SelftestError, match="Falta Deno"):
        selftest._check_deno(None)
    with pytest.raises(selftest.SelftestError, match="Deno no arranca"):
        selftest._check_deno(str(tmp_path / "no-such-deno.exe"))


def test_selftest_fails_when_detector_breaks(monkeypatch):
    from baitd.clip import framing
    def boom(size):
        raise RuntimeError("modelo ausente")
    monkeypatch.setattr(framing, "_make_detector", boom)
    with pytest.raises(selftest.SelftestError, match="detector de caras"):
        selftest._check_clip_deps()
