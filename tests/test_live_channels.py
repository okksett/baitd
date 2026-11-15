import pytest
import requests

from baitd.live import channels
from baitd.live.channels import Channel, LiveStatus, Offline
from baitd.models import UserError


@pytest.mark.parametrize("text,expected", [
    ("kick.com/Westcol", ("kick", "westcol", "https://kick.com/westcol")),
    ("https://www.twitch.tv/LofiGirl?x=1", ("twitch", "lofigirl", "https://www.twitch.tv/lofigirl")),
    ("youtube.com/@LofiGirl", ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")),
    ("https://m.youtube.com/@LofiGirl/live", ("youtube", "@LofiGirl", "https://www.youtube.com/@LofiGirl/live")),
    ("youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx",
     ("youtube", "UCxxxxxxxxxxxxxxxxxxxxxx", "https://www.youtube.com/channel/UCxxxxxxxxxxxxxxxxxxxxxx/live")),
    ("mira https://kick.com/Westcol.", ("kick", "westcol", "https://kick.com/westcol")),
])
def test_parse_channel(text, expected):
    assert channels.parse_channel(text) == expected


@pytest.mark.parametrize("text", ["kick.com/", "tiktok.com/@x", "twitch.tv/videos/123", "hola", "",
                                  "https://www.youtube.com/watch?v=abc", "kick.com/a"])
def test_parse_channel_rejects(text):
    with pytest.raises(UserError) as e:
        channels.parse_channel(text)
    assert e.value.cause == "Ese enlace no es de un canal de Kick, Twitch o YouTube"
    assert e.value.fix == "Pega el enlace del canal, por ejemplo kick.com/tu_canal"


def test_store_roundtrip_and_duplicates(tmp_data):
    assert channels.load_channels() == []
    a = channels.add_channel("kick.com/Westcol", mine=True)
    b = channels.add_channel("twitch.tv/lofigirl", mine=False)
    assert (a.platform, a.handle, a.mine, a.framing, a.delay_s) == ("kick", "westcol", True, "cara", None)
    assert a.id != b.id
    assert [c.to_dict() for c in channels.load_channels()] == [a.to_dict(), b.to_dict()]
    with pytest.raises(UserError) as e:
        channels.add_channel("https://www.kick.com/WESTCOL", mine=False)
    assert e.value.cause == "Ese canal ya está en la lista"
    yt = channels.add_channel("youtube.com/@LofiGirl", mine=False)
    with pytest.raises(UserError):
        channels.add_channel("youtube.com/@lofigirl/live", mine=False)
    assert Channel.from_dict(yt.to_dict()) == yt


def test_from_dict_sanitises():
    c = Channel.from_dict({"id": "x", "platform": "kick", "handle": "h", "url": "u",
                           "framing": "nope", "delay_s": 999})
    assert c.framing == "cara" and c.delay_s is None
    assert Channel.from_dict({"id": "x", "platform": "kick", "handle": "h", "url": "u",
                              "framing": "partido", "delay_s": 5}).delay_s == 5.0


def test_max_channels(tmp_data, monkeypatch):
    monkeypatch.setattr(channels, "MAX_CHANNELS", 2)
    channels.add_channel("kick.com/aaa", mine=False)
    channels.add_channel("kick.com/bbb", mine=False)
    with pytest.raises(UserError):
        channels.add_channel("kick.com/ccc", mine=False)


def test_corrupt_store_is_empty(tmp_data):
    (tmp_data / "canales.json").write_text("{nope", encoding="utf-8")
    assert channels.load_channels() == []


class _Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self.payload


class _Http:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


KICK = Channel("1", "kick", "westcol", "https://kick.com/westcol")


def test_kick_status():
    live = {"user": {"username": "Westcol"}, "livestream": {"session_title": "Hola", "viewer_count": 1261}}
    http = _Http(_Resp({"user": {"username": "Westcol"}, "livestream": None}), _Resp(live))
    off = channels.live_status(KICK, http=http)
    assert off.live is False and off.checked_at > 0
    on = channels.live_status(KICK, http=http)
    assert (on.live, on.title, on.viewers, on.name) == (True, "Hola", 1261, "Westcol")
    url, kw = http.calls[0]
    assert url == "https://kick.com/api/v2/channels/westcol"
    assert kw["headers"] == {"Accept": "application/json", "User-Agent": "Mozilla/5.0"} and kw["timeout"] == 15


def test_kick_failure_is_offline_and_logs_no_query(caplog):
    http = _Http(requests.ConnectionError("https://kick.com/api/v2/channels/westcol?token=SECRET"))
    with caplog.at_level("WARNING"):
        assert channels.live_status(KICK, http=http).live is False
    assert "SECRET" not in caplog.text and caplog.records


def test_ytdlp_status_offline_and_live():
    tw = Channel("2", "twitch", "lofigirl", "https://www.twitch.tv/lofigirl")
    answers = [Exception("lofigirl: The channel is not currently live"), {"is_live": True, "title": "t"},
               {"is_live": False, "title": "t"}, Exception("boom ?token=SECRET")]

    def extract(url):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    assert channels.live_status(tw, extract=extract).live is False
    on = channels.live_status(tw, extract=extract)
    assert (on.live, on.title, on.viewers, on.name) == (True, "t", None, "")
    assert channels.live_status(tw, extract=extract).live is False
    assert channels.live_status(tw, extract=extract).live is False


def _f(h, tbr, proto="m3u8_native", **kw):
    return {"url": f"https://x/{h}-{tbr}.m3u8", "protocol": proto, "height": h, "tbr": tbr, **kw}


def test_pick_format():
    info = {"formats": [
        _f(160, 300, vcodec="avc1", acodec="mp4a"), _f(720, 3000, vcodec="avc1"),
        _f(1080, 6000, vcodec="avc1"), _f(1080, 4000, vcodec="avc1"),
        _f(1080, 9000, proto="https", vcodec="avc1", acodec="none"),
        _f(1080, 9999, vcodec="avc1", acodec="none"),
        _f(1440, 12000, vcodec="avc1", acodec="mp4a")]}
    assert channels.pick_format(info, 1080)["url"] == "https://x/1080-6000.m3u8"
    assert channels.pick_format(info, 720)["height"] == 720
    with pytest.raises(UserError) as e:
        channels.pick_format(info, 100)
    assert e.value.cause == "No se encontró el video del directo"


def test_stream_url_and_offline():
    yt = Channel("3", "youtube", "@x", "https://www.youtube.com/@x/live")
    info = {"is_live": True, "formats": [_f(720, 2000, vcodec="avc1", acodec="mp4a")]}
    assert channels.stream_url(yt, 1080, extract=lambda u: info) == ("https://x/720-2000.m3u8", info)
    with pytest.raises(Offline):
        channels.stream_url(yt, 1080, extract=lambda u: {"is_live": False})

    def boom(u):
        raise Exception("ERROR: UserNotLive")
    with pytest.raises(Offline):
        channels.stream_url(yt, 1080, extract=boom)


@pytest.mark.network
def test_network_kick_live_url():
    from baitd.live.hls import parse_media_playlist
    r = requests.get("https://kick.com/stream/featured-livestreams/es",
                     headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"}, timeout=15)
    slug = r.json()["data"][0]["channel"]["slug"]
    ch = Channel("n", "kick", slug, f"https://kick.com/{slug}")
    url, info = channels.stream_url(ch, 720)
    text = requests.get(url, timeout=15).text
    assert len(parse_media_playlist(text, url).segments) >= 1


@pytest.mark.network
def test_network_twitch_lofigirl_status():
    ch = Channel("t", "twitch", "lofigirl", "https://www.twitch.tv/lofigirl")
    st = channels.live_status(ch)
    assert isinstance(st, LiveStatus)
