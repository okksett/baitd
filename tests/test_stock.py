import os

import pytest
import requests

from baitd import config

from baitd.llm._http import AuthError
from baitd.models import Media, UserError
from baitd.stock import cache, pexels, pixabay
from baitd.stock.select import find_media, passes_quality, score


def media(w, h, id="pexels:v:1", dur=10.0, kind="video"):
    return Media(id, id.split(":")[0], kind, "http://u/" + id, w, h, None if kind == "photo" else dur)


class Resp:
    def __init__(self, data=None, status=200, chunks=(b"abc", b"def")):
        self.status_code = status
        self._data = data
        self._chunks = chunks
        self.text = ""

    def json(self):
        return self._data

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def close(self):
        pass


def test_quality_rule():
    assert passes_quality(media(1080, 1920), 1080, 1920) and passes_quality(media(2160, 3840), 1080, 1920)
    assert not passes_quality(media(1920, 1080), 1080, 1920)  # needs 1.78x upscale
    assert passes_quality(media(3840, 2160), 1080, 1920)  # 4K landscape crop is fine
    assert not passes_quality(media(900, 1600), 1080, 1920)  # 1.2 * 1.08 > 1.25
    assert not passes_quality(media(0, 0), 1080, 1920)


def test_score_prefers_orientation_and_length():
    port = media(1080, 1920, dur=5)
    land = media(3840, 2160, dur=5)
    assert score(port, 1080, 1920, 2.0) > score(land, 1080, 1920, 2.0)
    short = media(1080, 1920, dur=1.0)
    assert score(port, 1080, 1920, 2.0) > score(short, 1080, 1920, 2.0)
    photo = media(2160, 3840, id="pexels:p:1", kind="photo")
    assert score(photo, 1080, 1920, 2.0) < score(port, 1080, 1920, 2.0)


PEXELS_VIDEO = {"videos": [{
    "id": 123, "width": 2160, "height": 3840, "duration": 12,
    "video_files": [
        {"id": 1, "quality": "hls", "file_type": "application/x-mpegURL", "width": None, "height": None, "link": "h"},
        {"id": 2, "quality": "uhd", "file_type": "video/mp4", "width": 2160, "height": 3840, "link": "uhd"},
        {"id": 3, "quality": "hd", "file_type": "video/mp4", "width": 1080, "height": 1920, "link": "hd"},
        {"id": 4, "quality": "sd", "file_type": "video/mp4", "width": 540, "height": 960, "link": "sd"},
    ]}, {
    "id": 124, "width": 640, "height": 360, "duration": 5,
    "video_files": [{"id": 5, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "link": "x"}],
}]}


def test_pexels_videos_pick_smallest_passing(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen.clear()
        seen.update(kw)
        return Resp(PEXELS_VIDEO)

    monkeypatch.setattr(requests, "get", fake_get)
    out = pexels.search_videos("book", "portrait", {"pexels_key": "K"}, out_w=1080, out_h=1920)
    assert [m.id for m in out] == ["pexels:v:123"]
    assert out[0].url == "hd" and out[0].width == 1080 and out[0].duration == 12
    assert seen["headers"]["Authorization"] == "K"
    assert seen["params"]["orientation"] == "portrait"
    pexels.search_videos("book", "any", {"pexels_key": "K"})
    assert "orientation" not in seen["params"]


def test_pexels_photos(monkeypatch):
    data = {"photos": [{"id": 9, "width": 4000, "height": 6000,
                        "src": {"original": "o", "large2x": "l2", "large": "l"}}]}
    monkeypatch.setattr(requests, "get", lambda url, **kw: Resp(data))
    out = pexels.search_photos("book", "portrait", {"pexels_key": "K"}, out_w=1080, out_h=1920)
    assert out[0].id == "pexels:p:9" and out[0].kind == "photo" and out[0].url == "o"


def test_pixabay_videos_and_key_not_leaked(monkeypatch):
    data = {"hits": [{"id": 7, "duration": 9, "videos": {
        "large": {"url": "L", "width": 3840, "height": 2160},
        "medium": {"url": "M", "width": 1920, "height": 1080},
        "small": {"url": "S", "width": 1280, "height": 720},
        "tiny": {"url": "", "width": 0, "height": 0}}}]}
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        return Resp(data)

    monkeypatch.setattr(requests, "get", fake_get)
    out = pixabay.search_videos("book", "portrait", {"pixabay_key": "SECRET"}, out_w=1080, out_h=1920)
    assert out[0].id == "pixabay:v:7" and out[0].url == "L"  # only 4K passes for portrait output
    assert seen["params"]["key"] == "SECRET"

    def boom(url, **kw):
        raise requests.exceptions.InvalidURL("http://x/?key=SECRET")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(UserError) as e:
        pixabay.search_videos("book", "any", {"pixabay_key": "SECRET"})
    assert "SECRET" not in str(e.value)


def test_pixabay_photos(monkeypatch):
    data = {"hits": [{"id": 3, "imageWidth": 4000, "imageHeight": 6000,
                      "largeImageURL": "big", "fullHDURL": "hd"}]}
    seen = {}

    def fake_get(url, **kw):
        seen.update(kw)
        return Resp(data)

    monkeypatch.setattr(requests, "get", fake_get)
    out = pixabay.search_photos("book", "portrait", {"pixabay_key": "K"}, out_w=1080, out_h=1920)
    assert out[0].id == "pixabay:p:3" and out[0].url == "hd" and out[0].kind == "photo"
    assert seen["params"]["orientation"] == "vertical" and seen["params"]["image_type"] == "photo"


def patch_providers(monkeypatch, videos=None, photos=None):
    videos = videos or (lambda q, o, s, **kw: [])
    photos = photos or (lambda q, o, s, **kw: [])
    for mod in (pexels, pixabay):
        monkeypatch.setattr(mod, "search_videos", videos)
        monkeypatch.setattr(mod, "search_photos", photos)


def test_find_media_skips_used_and_unique(monkeypatch):
    a, b, c = (media(1080, 1920, id=f"pexels:v:{i}") for i in (1, 2, 3))
    patch_providers(monkeypatch, videos=lambda q, o, s, **kw: [a, b, c, a])
    used = {"pexels:v:1"}
    out = find_media(["book"], 2, 2.0, 1080, 1920, used, {"pexels_key": "k"})
    assert [m.id for m in out] == ["pexels:v:2", "pexels:v:3"]
    assert used == {"pexels:v:1"}


def test_find_media_drops_failing_quality(monkeypatch):
    bad = media(640, 360, id="pexels:v:1")
    good = media(1080, 1920, id="pexels:v:2")
    patch_providers(monkeypatch, videos=lambda q, o, s, **kw: [bad, good])
    out = find_media(["book"], 2, 2.0, 1080, 1920, set(), {"pexels_key": "k"})
    assert [m.id for m in out] == ["pexels:v:2"]


def test_select_falls_back_to_photo(monkeypatch):
    calls = []

    def videos(q, o, s, **kw):
        calls.append((q, o))
        return [media(640, 360, id="pexels:v:1")]

    photo = media(2160, 3840, id="pexels:p:1", kind="photo")
    patch_providers(monkeypatch, videos=videos, photos=lambda q, o, s, **kw: [photo])
    out = find_media(["old book", "library"], 1, 2.0, 1080, 1920, set(), {"pexels_key": "k"})
    assert [m.kind for m in out] == ["photo"]
    assert ("old book", "portrait") in calls and ("library", "any") in calls and ("old", "any") in calls


def test_nothing_found_message(monkeypatch):
    patch_providers(monkeypatch)
    with pytest.raises(UserError, match="book") as e:
        find_media(["book"], 1, 2.0, 1080, 1920, set(), {"pexels_key": "k"})
    assert "Pexels/Pixabay" in str(e.value)


def test_missing_key_message():
    with pytest.raises(UserError, match="Pexels"):
        find_media(["book"], 1, 2.0, 1080, 1920, set(), {})


def test_download_uses_cache(monkeypatch, tmp_data):
    n = {"c": 0}

    def fake_get(url, **kw):
        n["c"] += 1
        assert kw["stream"] is True and kw["timeout"]
        return Resp()

    monkeypatch.setattr(requests, "get", fake_get)
    m = media(1080, 1920, id="pexels:v:1")
    m.url = "http://cdn/x.mp4?token=1"
    p = cache.download(m)
    assert p.read_bytes() == b"abcdef" and p.parent == tmp_data / "cache" and p.name == "pexels_video_1_1080x1920.mp4"
    assert not list(p.parent.glob("*.part"))
    assert cache.download(m) == p and n["c"] == 1 and m.path == p


def test_cache_path_depends_on_rendition(tmp_data):
    # I2: the same clip in two renditions (9:16 vs 16:9 picks) must not share a cache file
    small, big = media(1080, 1920, id="pexels:v:7"), media(2160, 3840, id="pexels:v:7")
    assert cache.cache_path(small) != cache.cache_path(big)
    assert "1080x1920" in cache.cache_path(small).name and "2160x3840" in cache.cache_path(big).name


def test_cache_hit_touches_the_file(monkeypatch, tmp_data):
    monkeypatch.setattr(requests, "get", lambda url, **kw: Resp())
    m = media(1080, 1920, id="pexels:v:3")
    p = cache.download(m)
    os.utime(p, (1_000_000, 1_000_000))
    assert cache.download(m) == p
    assert p.stat().st_mtime > 1_000_000 + 1000  # recently used again


def _cached(name, size, mtime):
    d = config.data_dir() / "cache"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_bytes(b"x" * size)
    os.utime(f, (mtime, mtime))
    return f


def test_prune_evicts_least_recently_used(tmp_data):
    old = _cached("pexels_video_1_1080x1920.mp4", 400, 1_000)
    mid = _cached("pexels_video_2_1080x1920.mp4", 400, 2_000)
    new = _cached("pexels_video_3_1080x1920.mp4", 400, 3_000)
    cache.prune(1000)  # 1200 bytes stored, cap 1000: only the oldest has to go
    assert not old.exists() and mid.exists() and new.exists()
    cache.prune(400)
    assert not mid.exists() and new.exists()


def test_prune_without_cache_folder_is_a_no_op(tmp_data):
    cache.prune(10)
    assert not (tmp_data / "cache").exists()


@pytest.mark.parametrize("value, expected", [(None, 3.0), ("", 3.0), ("bad", 3.0), (-1, 3.0), (0, 3.0),
                                             (1.5, 1.5), ("2", 2.0)])
def test_cache_cap_from_settings(value, expected):
    settings = {} if value is None else {"cache_max_gb": value}
    assert cache.max_bytes(settings) == int(expected * 1024 ** 3)


def test_download_error_is_user_error(monkeypatch, tmp_data):
    monkeypatch.setattr(requests, "get", lambda url, **kw: Resp(status=500))
    with pytest.raises(UserError):
        cache.download(media(1080, 1920, id="pexels:v:2"))
    assert not list((tmp_data / "cache").glob("*"))


def per_provider(monkeypatch, pex, pix):
    """pex/pix: callables (query, orient, settings, **kw) -> list; photos return nothing."""
    monkeypatch.setattr(pexels, "search_videos", pex)
    monkeypatch.setattr(pixabay, "search_videos", pix)
    for mod in (pexels, pixabay):
        monkeypatch.setattr(mod, "search_photos", lambda q, o, s, **kw: [])


KEYS = {"pexels_key": "k", "pixabay_key": "k"}


def test_pixabay_not_called_when_pexels_fills_need(monkeypatch):
    pix_calls = []
    a = media(1080, 1920, id="pexels:v:1")
    per_provider(monkeypatch, lambda q, o, s, **kw: [a],
                 lambda q, o, s, **kw: pix_calls.append(q) or [])
    out = find_media(["book"], 1, 2.0, 1080, 1920, set(), KEYS)
    assert [m.id for m in out] == ["pexels:v:1"] and pix_calls == []


def test_pixabay_fills_remainder_after_pexels(monkeypatch):
    a = media(1080, 1920, id="pexels:v:1", dur=2.0)
    b = media(1080, 1920, id="pixabay:v:9", dur=60.0)  # better score, still comes after Pexels
    per_provider(monkeypatch, lambda q, o, s, **kw: [a], lambda q, o, s, **kw: [b])
    out = find_media(["book"], 2, 2.0, 1080, 1920, set(), KEYS)
    assert [m.id for m in out] == ["pexels:v:1", "pixabay:v:9"]


def test_auth_failed_provider_is_skipped_later(monkeypatch):
    pex_calls = []

    def pex(q, o, s, **kw):
        pex_calls.append(q)
        raise AuthError("La clave de Pexels no es válida", "Revísala en Ajustes")

    b = media(1080, 1920, id="pixabay:v:9")
    c = media(1080, 1920, id="pixabay:v:10")
    seq = iter([[b], [c]])
    per_provider(monkeypatch, pex, lambda q, o, s, **kw: next(seq, []))
    out = find_media(["book", "page"], 2, 2.0, 1080, 1920, set(), KEYS)
    assert [m.id for m in out] == ["pixabay:v:9", "pixabay:v:10"]
    assert len(pex_calls) == 1


def test_pixabay_only_key(monkeypatch):
    pex_calls = []
    b = media(1080, 1920, id="pixabay:v:9")
    per_provider(monkeypatch, lambda q, o, s, **kw: pex_calls.append(q) or [],
                 lambda q, o, s, **kw: [b])
    out = find_media(["book"], 1, 2.0, 1080, 1920, set(), {"pixabay_key": "k"})
    assert [m.id for m in out] == ["pixabay:v:9"] and pex_calls == []


def test_pixabay_bad_key_400_is_an_auth_error(monkeypatch):
    # M1: Pixabay answers a bad key with 400 + "Invalid or missing API key"
    r = Resp(status=400)
    r.text = "[ERROR 400] Invalid or missing API key (https://pixabay.com/api/docs/)."
    monkeypatch.setattr(requests, "get", lambda url, **kw: r)
    with pytest.raises(AuthError) as e:
        pixabay.search_videos("book", "any", {"pixabay_key": "secret-k"})
    assert e.value.cause == "La clave de Pixabay no es válida" and "secret-k" not in str(e.value)


def test_auth_error_type_marks_provider_dead_whatever_the_text(monkeypatch):
    pix_calls = []

    def pix(q, o, s, **kw):
        pix_calls.append(q)
        raise AuthError("cualquier texto", "Revísala en Ajustes")

    per_provider(monkeypatch, lambda q, o, s, **kw: [], pix)
    with pytest.raises(AuthError):
        find_media(["book", "page"], 1, 2.0, 1080, 1920, set(), KEYS)
    assert len(pix_calls) == 1  # not queried again at every stage


def test_download_http_error_has_fixed_fix_text(monkeypatch, tmp_data):
    # M2: a dead CDN file is not a key or model problem
    for status in (403, 404):
        monkeypatch.setattr(requests, "get", lambda url, status=status, **kw: Resp(status=status))
        with pytest.raises(UserError) as e:
            cache.download(media(1080, 1920, id=f"pexels:v:{status}"))
        assert e.value.cause == "No se pudo descargar un clip de Pexels"
        assert e.value.fix == "Inténtalo de nuevo; si sigue fallando, prueba otro tema"
