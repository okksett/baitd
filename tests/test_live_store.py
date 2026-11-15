import json
from types import SimpleNamespace

import pytest

from baitd.clip import pipeline as clip_pipeline
from baitd.clip import sources
from baitd.live import store
from baitd.live.buffer import Cut


@pytest.fixture
def cut(make_mp4, tmp_path):
    return Cut(make_mp4(seconds=12, w=320, h=180, fps=15), 3.0, 9.0, 12.0, False)


def _channel(**kw):
    return SimpleNamespace(**{"platform": "kick", "handle": "westcol", "mine": False, "framing": "cara", **kw})


def _save(cut, channel=None, **kw):
    args = dict(channel=channel or _channel(), title="Gran jugada", reason="manual", at=1_700_000_000.0,
                session_id="s1", framing="cara")
    return store.save_moment(cut, **{**args, **kw})


def test_save_and_list(tmp_data, cut):
    aid = _save(cut)
    first = store.list_moments()[0]
    assert first["analysis_id"] == aid
    assert first["reason"] == "manual" and first["platform"] == "kick" and first["handle"] == "westcol"
    assert first["has_thumb"] is True and first["gap"] is False and first["mine"] is False
    assert first["classified"] is False and first["kind"] == "interesante"
    assert first["title"] == "Gran jugada" and first["duration"] == pytest.approx(12.0, abs=0.5)
    assert store.thumb_path(aid).is_file()
    m = clip_pipeline.load_analysis(aid)["moments"][0]
    assert m["credit"] == "@westcol" and m["badge"] == "kick" and m["cam"] is None
    assert (m["start"], m["end"]) == (3.0, 9.0)
    src = sources.load_source(first["source_id"])
    assert src.channel == "" and src.title == "Gran jugada" and src.path.name == "video.mp4"
    assert not cut.path.exists()  # the file moved into the source folder


def test_list_newest_first(tmp_data, make_mp4):
    a = _save(Cut(make_mp4(seconds=4, w=320, h=180, fps=15), 1.0, 3.0, 4.0, False), at=1_700_000_000.0)
    b = _save(Cut(make_mp4(seconds=4, w=320, h=180, fps=15), 1.0, 3.0, 4.0, False), at=1_700_000_100.0)
    assert [e["analysis_id"] for e in store.list_moments()] == [b, a]


def test_own_channel_has_no_credit(tmp_data, cut):
    aid = _save(cut, _channel(mine=True))
    m = clip_pipeline.load_analysis(aid)["moments"][0]
    assert m["credit"] == "" and m["badge"] == ""


def test_handle_with_at_and_youtube_channel_id(tmp_data, make_mp4):
    def mk():
        return Cut(make_mp4(seconds=4, w=320, h=180, fps=15), 1.0, 3.0, 4.0, False)

    uc = "UC" + "a" * 22
    a = _save(mk(), _channel(handle="@westcol"))
    b = _save(mk(), _channel(platform="youtube", handle=uc), display_name="Canal Bonito")
    c = _save(mk(), _channel(platform="youtube", handle=uc))
    credit = lambda i: clip_pipeline.load_analysis(i)["moments"][0]["credit"]
    assert credit(a) == "@westcol"
    assert credit(b) == "Canal Bonito"
    assert credit(c) == uc


def test_analysis_shape(tmp_data, cut):
    aid = _save(cut, kind="gracioso", classified=True, description="d", hashtags=["#a"], text="hola")
    raw = json.loads((clip_pipeline.analyses_dir() / f"{aid}.json").read_text("utf-8"))
    assert raw["origin"] == "live"
    assert raw["transcript"] == {"language": "", "words": [], "duration": 12.0}
    assert raw["live"]["session"] == "s1" and raw["live"]["classified"] is True and raw["live"]["gap"] is False
    assert raw["moments"][0]["hashtags"] == ["#a"] and raw["moments"][0]["kind"] == "gracioso"


def test_bad_reason_and_framing_rejected(tmp_data, cut):
    with pytest.raises(ValueError):
        _save(cut, reason="nope")
    with pytest.raises(ValueError):
        _save(cut, framing="raro")
    assert list(sources.sources_dir().iterdir()) == []


def test_update_moment_rejects_other_fields(tmp_data, cut):
    aid = _save(cut)
    with pytest.raises(ValueError):
        store.update_moment(aid, start=1)
    store.update_moment(aid, title="X", kind="gracioso", classified=True, hashtags=["#x"], description="dd",
                        transcript={"language": "es", "words": [{"text": "hola", "start": 0.0, "end": 0.5}],
                                    "duration": 12.0})
    data = clip_pipeline.load_analysis(aid)
    assert data["moments"][0]["title"] == "X" and data["moments"][0]["kind"] == "gracioso"
    assert data["moments"][0]["hashtags"] == ["#x"] and data["moments"][0]["description"] == "dd"
    assert data["live"]["classified"] is True and data["transcript"]["language"] == "es"
    assert store.list_moments()[0]["title"] == "X"


def test_update_moment_unknown_id(tmp_data):
    with pytest.raises(Exception):
        store.update_moment("0" * 12, title="X")


def test_sweep_keeps_live_analyses(tmp_data, cut, make_mp4):
    aid = _save(cut)
    sid = store.list_moments()[0]["source_id"]
    plain_src = sources.finish_upload(*_plain_upload(make_mp4))
    plain = clip_pipeline.analyses_dir() / ("f" * 12 + ".json")
    plain.write_text(json.dumps({"source_id": plain_src.id}), encoding="utf-8")
    clip_pipeline.sweep(set(), set(), keep_sources=False)
    assert (clip_pipeline.analyses_dir() / f"{aid}.json").is_file()
    assert (sources.sources_dir() / sid).is_dir()
    assert not plain.exists()
    assert not (sources.sources_dir() / plain_src.id).exists()


def _plain_upload(make_mp4):
    sid, path = sources.new_upload("a.mp4")
    import shutil
    shutil.copy(make_mp4(seconds=2, w=320, h=180, fps=15), path)
    return sid, path, "a.mp4"
