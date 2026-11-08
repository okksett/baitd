import json
import subprocess
import sys

import pytest

from baitd.clip.models import ClipProfile, Moment, SourceInfo, Transcript
from baitd.models import Profile, Word

# reuse the Flask `client` fixture (and what it depends on) from the server tests
from test_server import app, client, queue, runner  # noqa: F401


def test_old_profile_gets_default_clipping():
    p = Profile.from_dict({"id": "a", "name": "A"})
    assert p.clipping == ClipProfile()


def test_profile_clipping_roundtrips_as_plain_json():
    p = Profile.from_dict({"id": "a", "name": "A", "clipping": {"count": 7, "kind": "gracioso"}})
    assert p.clipping.count == 7 and p.clipping.kind == "gracioso"
    d = json.loads(json.dumps(p.to_dict()))
    assert d["clipping"]["count"] == 7
    assert Profile.from_dict(d) == p


def test_clip_profile_rejects_bad_values():
    for bad in ({"count": 0}, {"count": 21}, {"min_s": 9}, {"max_s": 181}, {"min_s": 60, "max_s": 30},
                {"kind": "x"}, {"framing": "x"}, {"transcriber": "x"},
                {"whisper_model": "tiny"}, {"language": "Spanish"}, {"language": "ES"}, {"language": ""}):
        with pytest.raises(ValueError):
            ClipProfile.from_dict(bad)


def test_clip_profile_rejects_wrong_types():
    for bad in ({"count": "5"}, {"count": True}, {"count": 5.0}, {"min_s": "20"}, {"hook_title": 1},
                {"credit": "yes"}, {"music": None}, {"kind": 3}, {"language": 5}):
        with pytest.raises(ValueError):
            ClipProfile.from_dict(bad)
    with pytest.raises(ValueError):
        ClipProfile.from_dict("nope")


def test_clip_profile_accepts_good_values_and_ignores_unknown_keys():
    c = ClipProfile.from_dict({"count": 20, "min_s": 10, "max_s": 10, "kind": "historia", "framing": "completo",
                               "transcriber": "openai", "whisper_model": "large-v3", "language": "es",
                               "extra": 1})
    assert (c.count, c.kind, c.language, c.whisper_model) == (20, "historia", "es", "large-v3")
    assert ClipProfile.from_dict(c.to_dict()) == c


def test_moment_roundtrip():
    m = Moment(1.0, 31.5, "gracioso", 87, "Nadie esperaba esto", "d", ["#a"], "texto")
    assert Moment.from_dict(m.to_dict()) == m


def test_transcript_roundtrip():
    t = Transcript("es", [Word("hola", 1.0, 1.4)], 10.0)
    assert Transcript.from_dict(t.to_dict()) == t


def test_source_info_roundtrip(tmp_path):
    s = SourceInfo("a" * 12, tmp_path / "v.mp4", "T", "", "", 12.5, 1920, 1080, 30.0, True)
    assert SourceInfo.from_dict(s.to_dict()) == s


@pytest.mark.parametrize("order", ["baitd.models, baitd.clip.models", "baitd.clip.models, baitd.models"])
def test_imports_work_in_either_order(order):
    r = subprocess.run([sys.executable, "-c", "import " + order], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_api_rejects_bad_clipping(client):
    r = client.post("/api/profiles", json={"id": "a", "name": "A", "clipping": {"count": 99}})
    assert r.status_code == 400 and "→" in r.get_json()["error"]
    assert "clipping" in r.get_json()["error"]


def test_api_saves_valid_clipping(client):
    r = client.post("/api/profiles", json={"id": "a", "name": "A", "clipping": {"count": 3}})
    assert r.status_code == 200
    assert r.get_json()["clipping"]["count"] == 3
