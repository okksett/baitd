"""clip.chat_replay and the chat path of a Clipping analysis: a finished stream's moments from its chat peaks."""
import json
import threading
import wave

import pytest

from baitd import config
from baitd.clip import chat_replay, pipeline, sources
from baitd.clip.models import Transcript
from baitd.models import UserError, Word


def _chat(length=300, laugh_at=120.0, burst_at=220.0):
    """Two quiet messages every 10 s, a burst of laughter at `laugh_at` and a plain burst at `burst_at`."""
    msgs = [(t + 1.0, "hola") for t in range(0, length, 5)]
    msgs += [(laugh_at + i * 0.2, "JAJAJAJA") for i in range(40)]
    msgs += [(burst_at + i * 0.3, "que") for i in range(30)]
    return sorted(msgs)


def _twitch_file(msgs):
    return json.dumps({"video": {"id": "1"}, "comments": [
        {"content_offset_seconds": t, "commenter": {"display_name": "x"}, "message": {"body": m}} for t, m in msgs]})


def _youtube_file(msgs):
    lines = []
    for t, m in msgs:
        runs = [{"text": m}] if m != "😂" else [{"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}]
        lines.append(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(int(t * 1000)), "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {"message": {"runs": runs}}}}}]}}))
    return "\n".join(lines) + "\n"


def test_reads_twitchdownloader_and_youtube_files():
    msgs = _chat()
    assert chat_replay.parse(_twitch_file(msgs)) == msgs
    yt = chat_replay.parse(_youtube_file(msgs + [(299.0, "😂")]))
    assert yt[-1] == (299.0, "😂") and yt[:-1] == [(round(t, 3), m) for t, m in msgs]


@pytest.mark.parametrize("text", ["", "hola", "[1, 2]", '{"comments": [{"x": 1}]}', json.dumps({"comments": []})])
def test_refuses_other_files(text):
    with pytest.raises(UserError):
        chat_replay.parse(text)


def test_save_and_load(tmp_data):
    msgs = _chat()
    chat_id = chat_replay.save(msgs)
    assert chat_replay.load(chat_id) == [(round(t, 3), m) for t, m in msgs]
    for bad in ("../x", "f" * 12, 7):
        with pytest.raises(UserError):
            chat_replay.load(bad)


def test_peaks_are_the_bursts_strongest_first():
    found = chat_replay.peaks(_chat(), 5)
    assert [(p.t, p.reason) for p in found] == [(120.0, "laughs"), (220.0, "chat")]
    assert found[0].messages > found[1].messages
    assert [p.t for p in chat_replay.peaks(_chat(), 1)] == [120.0]
    assert chat_replay.peaks([], 3) == []


def test_peaks_too_close_count_once():
    msgs = _chat(burst_at=150.0)  # 30 s after the laughs
    assert [p.t for p in chat_replay.peaks(msgs, 5)] == [120.0]


# ---- the analysis ----

def _providers(make_mp4, words, reply):
    seen = {"transcribe": 0, "prompts": []}
    src_file = make_mp4(seconds=300, w=320, h=180, fps=5)

    def fetch_url(url, cancel, on_progress):
        sid, target = sources.new_upload("directo.mp4")
        target.write_bytes(src_file.read_bytes())
        return sources.finish_upload(sid, target, "Directo.mp4")

    def transcribe(audio, provider, language, settings, model, cancel, on_progress, on_model_load=None, **kw):
        seen["transcribe"] += 1
        with wave.open(str(audio)) as w:
            seen["audio_s"] = w.getnframes() / w.getframerate()
        return Transcript("es", words, seen["audio_s"])

    def complete(prompt):
        seen["prompts"].append(prompt)
        return reply

    p = pipeline.ClipProviders(fetch_url=fetch_url, transcribe=transcribe, complete=complete,
                               detect_faces=lambda *a: [])
    p.seen = seen
    return p


def _joined_words():
    """Sentences every 5 s through the joined audio of two 70 s windows (plus 1 s of silence)."""
    out = []
    for k in range(28):
        t = k * 5.0
        out += [Word("Esto", t, t + 0.5), Word("pasó.", t + 0.6, t + 1.2)]
    return out


def test_analysis_with_a_chat_reads_only_the_peaks(tmp_data, tmp_path, make_mp4):
    reply = '{"tipo":"humor","inicio":"0:10","fin":"0:40","titulo":"Se cae","descripcion":"Una caída.",' \
            '"hashtags":["risa"]}'
    providers = _providers(make_mp4, _joined_words(), reply)
    chat_id = chat_replay.save(_chat())
    item = {"id": "j" * 12, "topic": "x", "kind": "clip_analyze", "profile": config.default_profile().to_dict(),
            "payload": {"source": {"url": "https://example.test/v"}, "chat_id": chat_id,
                        "overrides": {"count": 2, "min_s": 10, "max_s": 60}}}
    path = pipeline.run_clip_analyze(item, {"output_dir": str(tmp_path / "out")}, threading.Event(),
                                     lambda *a: None, providers)
    analysis = pipeline.load_analysis(path.stem)
    assert providers.seen["transcribe"] == 1  # one model load for every window
    assert providers.seen["audio_s"] == pytest.approx(2 * 70.0 + 2 * 1.0, abs=0.1)  # 2 windows, not 300 s
    assert len(providers.seen["prompts"]) == 2
    ms = analysis["moments"]
    assert [m["title"] for m in ms] == ["Se cae", "Se cae"] and ms[0]["kind"] == "gracioso"
    assert ms[0]["score"] == 100 and ms[1]["score"] < 100
    starts = sorted(m["start"] for m in ms)
    assert 80.0 <= starts[0] < 130.0 and 180.0 <= starts[1] < 230.0  # on the video's clock, near each peak
    words = analysis["transcript"]["words"]
    assert words and all(80.0 <= w["start"] < 150.0 or 180.0 <= w["start"] < 250.0 for w in words)


def test_a_peak_without_an_ai_answer_is_still_a_moment(tmp_data, tmp_path, make_mp4):
    providers = _providers(make_mp4, _joined_words(), "no sé")
    item = {"id": "j" * 12, "topic": "x", "kind": "clip_analyze", "profile": config.default_profile().to_dict(),
            "payload": {"source": {"url": "https://example.test/v"}, "chat_id": chat_replay.save(_chat()),
                        "overrides": {"count": 1, "min_s": 10, "max_s": 60}}}
    path = pipeline.run_clip_analyze(item, {"output_dir": str(tmp_path / "out")}, threading.Event(),
                                     lambda *a: None, providers)
    ms = pipeline.load_analysis(path.stem)["moments"]
    assert {m["kind"] for m in ms} == {"gracioso", "interesante"}  # laughs, and a plain burst
    assert all(m["title"].startswith("Esto pasó.") for m in ms)


def test_a_youtube_replay_saved_with_the_video_is_used(tmp_data, tmp_path, make_mp4):
    providers = _providers(make_mp4, _joined_words(), "{}")
    fetch = providers.fetch_url

    def fetch_with_chat(url, cancel, on_progress):
        src = fetch(url, cancel, on_progress)
        (sources.Path(src.path).parent / "chat.live_chat.json").write_text(_youtube_file(_chat()), "utf-8")
        return src

    providers.fetch_url = fetch_with_chat
    item = {"id": "j" * 12, "topic": "x", "kind": "clip_analyze", "profile": config.default_profile().to_dict(),
            "payload": {"source": {"url": "https://example.test/v"}, "overrides": {"count": 1}}}
    pipeline.run_clip_analyze(item, {"output_dir": str(tmp_path / "out")}, threading.Event(), lambda *a: None,
                              providers)
    assert providers.seen["audio_s"] < 150.0  # the peaks' windows, not the whole video
