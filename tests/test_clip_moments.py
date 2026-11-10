import json
import logging
import subprocess
import threading

import pytest

from baitd import llm
from baitd.clip import moments
from baitd.clip.models import ClipProfile, Moment, Transcript
from baitd.config import default_profile
from baitd.ffmpeg import JobCancelled, ffmpeg_exe
from baitd.llm._http import ContentBlocked
from baitd.models import UserError, Word


def make_transcript(n_sentences=40, words_per=5, word_s=0.5, gap=0.1, punct=True, offset=0.0):
    """Sentence k spans [k*S, k*S + 2.5) where S = words_per*word_s + gap; absolute seconds."""
    words, t = [], offset
    for k in range(n_sentences):
        for j in range(words_per):
            last = j == words_per - 1
            text = f"s{k}w{j}" + ("." if last and punct else "")
            words.append(Word(text, t, t + word_s))
            t += word_s
        t += gap
    return Transcript(language="es", words=words, duration=t)


STEP = 5 * 0.5 + 0.1  # seconds per sentence of make_transcript


def sent_start(k):
    return k * STEP


def sent_end(k):
    return k * STEP + 2.5


def ts(sec):
    return f"{int(sec) // 60:02d}:{int(sec) % 60:02d}"


def item(a, b, kind="gracioso", score=80, title="Un titulo", hashtags=("tag",)):
    return {"start": ts(a), "end": ts(b), "kind": kind, "score": score, "title": title,
            "description": "d", "hashtags": list(hashtags)}


def reply(*items):
    return json.dumps({"moments": list(items)})


OPTS = ClipProfile(min_s=10, max_s=30)


def test_windows_overlap_and_cover_all():
    t = Transcript("es", [Word("a", 0, 1)], duration=3000.0)
    w = moments.windows(t)
    assert moments.WINDOW_S == 1200.0 and moments.OVERLAP_S == 60.0
    assert w[0] == (0.0, 1200.0)
    assert w[1][0] == 1140.0 and w[0][1] - w[1][0] == 60.0  # overlap
    assert w[-1][1] == 3000.0
    for (_, e1), (s2, _) in zip(w, w[1:]):
        assert s2 < e1  # no gaps
    assert moments.windows(Transcript("es", [], duration=100.0)) == [(0.0, 100.0)]


def test_snap_never_cuts_mid_sentence():
    t = make_transcript()
    # starts inside sentence 3, ends inside sentence 8
    s, e = moments.snap_to_sentences(sent_start(3) + 1.0, sent_end(8) - 0.7, t.words, 5, 60)
    assert s == pytest.approx(sent_start(3)) and e == pytest.approx(sent_end(8))
    # also without any punctuation: pauses >= 0.8 s are the boundaries
    quiet = make_transcript(punct=False, gap=1.0)
    s, e = moments.snap_to_sentences(1.0, 9.0, quiet.words, 5, 60)
    assert s == pytest.approx(0.0)
    boundaries = {round(w.end, 6) for w in quiet.words[4::5]}
    assert round(e, 6) in boundaries


def test_snap_respects_min_max_or_none():
    t = make_transcript()
    # too long: shrink from the end by whole sentences
    s, e = moments.snap_to_sentences(sent_start(0), sent_end(20), t.words, 5, 20)
    assert 5 <= e - s <= 20 and s == pytest.approx(0.0)
    assert any(e == pytest.approx(sent_end(k)) for k in range(20))
    # too short: grow forward by whole sentences
    s, e = moments.snap_to_sentences(sent_start(2), sent_end(2), t.words, 10, 30)
    assert s == pytest.approx(sent_start(2)) and 10 <= e - s <= 30
    assert any(e == pytest.approx(sent_end(k)) for k in range(40))
    # impossible: one sentence longer than max, and not enough transcript left to reach min
    assert moments.snap_to_sentences(0.0, 2.0, t.words, 1, 2.0) is None
    assert moments.snap_to_sentences(sent_start(39), sent_end(39), t.words, 20, 30) is None


def test_dedupe_keeps_higher_score(make_wav):
    t = make_transcript()
    a = item(sent_start(0), sent_end(5), score=60, title="bajo")
    b = item(sent_start(1), sent_end(6), score=90, title="alto")  # overlaps a a lot
    c = item(sent_start(20), sent_end(25), score=50, title="aparte")
    out = moments.find_moments(t, make_wav(2), OPTS, default_profile(), {}, 10, threading.Event(),
                               lambda f: None, complete=lambda p: reply(a, b, c))
    assert [m.title for m in out] == ["alto", "aparte"]


def test_kind_filter_and_top_n(make_wav):
    t = make_transcript()
    items = [item(sent_start(k * 6), sent_end(k * 6 + 4), kind=("gracioso" if k % 2 else "historia"),
                  score=50 + k, title=f"m{k}") for k in range(6)]
    items.append(item(sent_start(30), sent_end(34), kind="inventado", score=99, title="malo"))
    audio = make_wav(2)

    def run(opts, want):
        return moments.find_moments(t, audio, opts, default_profile(), {}, want, threading.Event(),
                                    lambda f: None, complete=lambda p: reply(*items))

    todos = run(OPTS, 3)
    assert [m.title for m in todos] == ["m5", "m4", "m3"]  # invalid kind dropped, top 3 by score
    only = run(ClipProfile(min_s=10, max_s=30, kind="gracioso"), 10)
    assert {m.kind for m in only} == {"gracioso"} and len(only) == 3
    # the prompt limits kinds too
    seen = []
    moments.find_moments(t, audio, ClipProfile(min_s=10, max_s=30, kind="historia"), default_profile(), {}, 1,
                         threading.Event(), lambda f: None, complete=lambda p: seen.append(p) or reply(*items))
    assert "kind is one of: historia." in seen[0] and "gracioso" not in seen[0]


def test_parse_moments_normalises_fields():
    t = make_transcript()
    raw = reply({**item(sent_start(2), sent_end(6), score=250, title="uno dos tres cuatro cinco seis siete ocho nueve diez",
                        hashtags=("a b", "#c", "")), "start": "0:00:06", "end": sent_end(6) - 0.3})
    [m] = moments.parse_moments("```json\n" + raw + "\n```", t, OPTS)
    assert m.score == 100 and m.title == "uno dos tres cuatro cinco seis siete ocho"
    assert m.hashtags == ["#ab", "#c"]
    assert m.text.startswith("s2w0") and m.text.endswith("s6w4.")
    assert m.start == pytest.approx(sent_start(2)) and m.end == pytest.approx(sent_end(6))
    with pytest.raises(ValueError):
        moments.parse_moments("nope", t, OPTS)
    with pytest.raises(ValueError):
        moments.parse_moments('{"other": 1}', t, OPTS)
    assert moments.parse_moments('{"moments": []}', t, OPTS) == []


def test_prompt_lines_use_hours_past_one_hour():
    assert moments._ts(75) == "01:15" and moments._ts(3725) == "1:02:05"
    p = moments.build_prompt("[00:00] hola", OPTS, "es")
    assert "[00:00] hola" in p and "payoff" in p and "10" in p and "30" in p


def test_bad_json_retried_then_window_skipped(make_wav, caplog):
    t = make_transcript()
    audio = make_wav(2)
    good = reply(item(sent_start(0), sent_end(5)))
    calls = []

    def flaky(p):
        calls.append(p)
        return "not json" if len(calls) == 1 else good

    out = moments.find_moments(t, audio, OPTS, default_profile(), {}, 5, threading.Event(), lambda f: None,
                               complete=flaky)
    assert len(calls) == 2 and "rejected" in calls[1] and len(out) == 1

    calls.clear()
    # a second window of garbage is skipped with a warning; the other window still counts
    long = make_transcript(n_sentences=600)  # ~ 2 windows
    n = len(moments.windows(long))
    assert n >= 2

    def per_window(p):
        calls.append(p)
        if "s0w0" in p:  # first window: always garbage
            return "garbage"
        return reply(item(sent_start(150), sent_end(155)))

    with caplog.at_level(logging.WARNING):
        out = moments.find_moments(long, audio, OPTS, default_profile(), {}, 5, threading.Event(),
                                   lambda f: None, complete=per_window)
    assert out and any("omite" in r.message for r in caplog.records)


def test_zero_moments_raises_no_speech(make_wav):
    t = make_transcript()
    with pytest.raises(UserError) as e:
        moments.find_moments(t, make_wav(2), OPTS, default_profile(), {}, 5, threading.Event(), lambda f: None,
                             complete=lambda p: reply())
    assert "No se encontraron momentos" in e.value.cause


def test_ideas_scale_with_window_length():
    assert moments.ideas_for(1200) == 7
    assert moments.ideas_for(480) == 3 and moments.ideas_for(30) == 3
    assert "up to 7 moments" in moments.build_prompt("[00:00] hola", OPTS, "es", moments.ideas_for(1200))


def test_blocked_window_is_retried_in_pieces(make_wav, caplog):
    t = make_transcript(n_sentences=600)  # 2 windows; s10 (26 s) lies only in the first piece of window 1
    assert len(moments.windows(t)) == 2
    pieces = moments._spans(*moments.windows(t)[0], moments.BLOCKED_WINDOW_S)
    calls = []

    def complete(p):
        calls.append(p)
        if "s10w0" in p:
            raise ContentBlocked("Gemini bloqueó el texto con su filtro de contenido (PROHIBITED_CONTENT)", "x")
        return reply(item(sent_start(500), sent_end(505)))

    with caplog.at_level(logging.WARNING):
        out = moments.find_moments(t, make_wav(2), OPTS, default_profile(), {}, 5, threading.Event(),
                                   lambda f: None, complete=complete)
    assert out  # the rest of the video still yields moments
    assert len(calls) == 2 + len(pieces)  # both windows, then window 1 again in pieces
    assert "up to 3 moments" in calls[1]  # pieces ask for fewer ideas
    msgs = [r.message for r in caplog.records]
    assert any("se reintenta en partes" in m for m in msgs) and any("se omite" in m for m in msgs)


def test_everything_blocked_explains_the_filter(make_wav):
    def complete(p):
        raise ContentBlocked("Gemini bloqueó el texto con su filtro de contenido (PROHIBITED_CONTENT)", "x")

    with pytest.raises(ContentBlocked):
        moments.find_moments(make_transcript(), make_wav(2), OPTS, default_profile(), {}, 5, threading.Event(),
                             lambda f: None, complete=complete)


def test_progress_per_window_and_cancel(make_wav):
    t = make_transcript(n_sentences=600)
    n = len(moments.windows(t))
    prog = []
    moments.find_moments(t, make_wav(2), OPTS, default_profile(), {}, 5, threading.Event(), prog.append,
                         complete=lambda p: reply(item(sent_start(0), sent_end(5))))
    assert len(prog) == n and prog[-1] == 1.0 and prog == sorted(prog) and 0 < prog[0] < 1

    cancel = threading.Event()
    calls = []

    def complete(p):
        calls.append(p)
        cancel.set()
        return reply(item(sent_start(0), sent_end(5)))

    with pytest.raises(JobCancelled):
        moments.find_moments(t, make_wav(2), OPTS, default_profile(), {}, 5, cancel, lambda f: None,
                             complete=complete)
    assert len(calls) == 1


def test_energy_bonus_range(tmp_path):
    # 20 s of quiet tone with a loud burst at 8..12 s
    wav = tmp_path / "burst.wav"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "sine=f=440:r=16000:d=20,volume='if(between(t,8,12),1.0,0.05)':eval=frame", "-ac", "1", wav],
                   check=True, stdin=subprocess.DEVNULL)
    curve = moments.energy_curve(wav, threading.Event())
    assert 19 <= len(curve) <= 21
    assert curve[10] > curve[2] + 10  # loud burst is clearly louder
    loud = Moment(8.0, 12.0, "gracioso", 50, "t", "d", [], "x")
    quiet = Moment(0.0, 4.0, "gracioso", 50, "t", "d", [], "x")
    assert 0 < moments.energy_bonus(loud, curve) <= 15
    assert moments.energy_bonus(quiet, curve) == 0
    assert moments.energy_bonus(loud, []) == 0
    assert moments.energy_bonus(loud, [-70.0] * 20) == 0  # flat curve: nothing stands out


def test_energy_curve_silence_is_minus_70(tmp_path):
    wav = tmp_path / "silence.wav"
    subprocess.run([ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "anullsrc=r=16000:cl=mono", "-t", "3", wav], check=True, stdin=subprocess.DEVNULL)
    assert set(moments.energy_curve(wav, threading.Event())) == {-70.0}


def test_generate_script_still_uses_complete(monkeypatch):
    prompts = []
    valid = json.dumps({
        "title": "T", "hook": "Hola mundo ya.",
        "scenes": [{"text": "Hola mundo ya.", "keywords": ["a"]}, {"text": "Dos.", "keywords": ["b"]}],
        "description": "d", "hashtags": ["#x"],
    })
    monkeypatch.setattr(llm, "complete", lambda prompt, profile, settings, cancel=None: prompts.append(prompt) or valid)
    script = llm.generate_script("tema", default_profile(), {})
    assert script.title == "T" and len(prompts) == 1 and "tema" in prompts[0]


@pytest.mark.parametrize("offset", [0.0, 3600.0 + 1.3])
def test_prompt_marks_round_trip(offset):
    """Echoing the prompt's own marks (first line, last line included) returns exactly those sentences."""
    t = make_transcript(n_sentences=40, offset=offset)
    sents = moments._sentences(t.words)
    opts = ClipProfile(min_s=5, max_s=60)
    lines = moments._window_lines(sents, t.words, 0.0, 1e9).splitlines()
    mark = lambda ln: ln[1:ln.index("]")]
    for k, j in [(2, 6), (3, 8), (10, 14), (21, 27), (30, 34)]:
        raw = reply({"start": mark(lines[k]), "end": mark(lines[j]), "kind": "gracioso", "score": 70,
                     "title": "t", "description": "d", "hashtags": []})
        [m] = moments.parse_moments(raw, t, opts)
        assert m.start == pytest.approx(sents[k][0]) and m.end == pytest.approx(sents[j][1]), (k, j)
    if offset:
        assert lines[0].startswith("[1:00:01]")
