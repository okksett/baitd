"""Chat readers: parsers on recorded frames, give-up behaviour, and a few real-network checks."""
import json
import threading
import time

import pytest
import requests

from baitd.live import chat
from baitd.live.channels import Channel


def test_twitch_privmsgs():
    raw = "@badge-info=;color=#FF0000 :u!u@u.tmi.twitch.tv PRIVMSG #chan :jajaja KEKW\r\nPING :tmi.twitch.tv\r\n"
    assert chat.twitch_privmsgs(raw) == (["jajaja KEKW"], True)
    assert chat.twitch_privmsgs(":tmi.twitch.tv 001 x :Welcome\r\n") == ([], False)


_KICK_DATA = ('{"id":"6c19072a-7b3a-4334-b62f-e3b795d6f568","chatroom_id":30120423,"content":"es verdad eso asjsa",'
              '"type":"message","created_at":"2026-11-15T15:51:39+00:00","sender":{"id":9952155,"username":"emogangg"}}')


def _kick_frame(event=r"App\Events\ChatMessageEvent", as_string=False):
    pub = {"event": event, "data": _KICK_DATA}
    return {"push": {"channel": "chatrooms.30120423.v2", "pub": {"data": json.dumps(pub) if as_string else pub}}}


def test_kick_text():
    assert chat.kick_text(_kick_frame()) == "es verdad eso asjsa"
    assert chat.kick_text(_kick_frame(as_string=True)) == "es verdad eso asjsa"
    assert chat.kick_text(_kick_frame(event=r"App\Events\MessageDeletedEvent")) is None
    assert chat.kick_text({}) is None
    assert chat.kick_text({"id": 2, "subscribe": {}}) is None


def test_youtube_parse():
    data = {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"invalidationContinuationData": {"continuation": "C2", "timeoutMs": 5000}}],
        "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": [{"text": "jajaja"}]}}}}},
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": [
                {"emoji": {"shortcuts": [":risa:"]}}, {"text": " ok"}]}}}}},
            {"addChatItemAction": {"item": {"liveChatMembershipItemRenderer": {}}}},
        ]}}}
    assert chat.youtube_parse(data) == (["jajaja", ":risa: ok"], "C2", 5000)
    assert chat.youtube_parse({}) == ([], None, 5000)


def test_reader_gives_up_after_5_failures():
    ch = Channel(id="1", platform="kick", handle="x", url="https://kick.com/x")
    events = []
    calls = []

    async def boom(reader):
        calls.append(1)
        raise OSError("no")

    r = chat.ChatReader(ch, lambda t, s: None, lambda k, d: events.append((k, d)), connect=boom, retry_wait=0.0)
    r.start()
    r.join(10)
    assert not r.is_alive()
    assert len(calls) == 5
    assert [k for k, _ in events] == ["chat_off"]
    assert "Kick" in events[0][1]["text"] and "audio" in events[0][1]["text"]


def test_stop_ends_thread_promptly():
    ch = Channel(id="1", platform="twitch", handle="x", url="https://twitch.tv/x")
    started = threading.Event()

    async def forever(reader):
        import asyncio
        started.set()
        await asyncio.sleep(3600)

    r = chat.make_reader(ch, lambda t, s: None, lambda k, d: None, "")
    r2 = chat.ChatReader(ch, lambda t, s: None, lambda k, d: None, connect=forever)
    r2.start()
    assert started.wait(5)
    t0 = time.time()
    r2.stop()
    r2.join(5)
    assert not r2.is_alive() and time.time() - t0 < 3
    assert isinstance(r, chat.ChatReader)


def _collect(ch, video_id="", seconds=60):
    got = []
    r = chat.make_reader(ch, lambda t, s: got.append(s), lambda k, d: None, video_id)
    r.start()
    end = time.time() + seconds
    while time.time() < end and not got:
        time.sleep(0.5)
    r.stop()
    r.join(10)
    return got


@pytest.mark.network
def test_network_kick_chat():
    r = requests.get("https://kick.com/stream/featured-livestreams/es", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    slugs = [d["channel"]["slug"] for d in r.json()["data"]]
    assert slugs
    ch = Channel(id="k", platform="kick", handle=slugs[0], url=f"https://kick.com/{slugs[0]}")
    assert len(_collect(ch)) >= 1


@pytest.mark.network
def test_network_twitch_chat():
    from baitd.live import channels
    for login in ("kaicenat", "ibai", "xqc", "auronplay", "hasanabi", "gaules", "lirik", "elxokas", "rubius", "knekro"):
        ch = Channel(id="t", platform="twitch", handle=login, url=f"https://twitch.tv/{login}")
        try:
            if channels.live_status(ch).live:
                break
        except Exception:
            continue
    else:
        pytest.skip("no big Twitch channel live")
    assert len(_collect(ch)) >= 1


@pytest.mark.network
def test_network_youtube_chat():
    import yt_dlp
    vid = None
    for h in ("@LofiGirl", "@Bloomberg", "@SkyNews", "@aljazeeraenglish", "@DWNews", "@NASA"):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "playlist_items": "1"}) as y:
                info = y.extract_info(f"https://www.youtube.com/{h}/live", download=False)
            if info.get("is_live"):
                vid = info["id"]
                break
        except Exception:
            continue
    if not vid:
        pytest.skip("no live YouTube channel found")
    ch = Channel(id="y", platform="youtube", handle="x", url="https://youtube.com/watch?v=" + vid)
    assert len(_collect(ch, vid)) >= 1
