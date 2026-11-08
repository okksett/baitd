import re
from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

from baitd.captions import (
    FONTS_DIR, PRESETS, caption_baseline_y, chunk_words, hex_to_ass, is_latin, write_ass,
)
from baitd.config import default_profile
from baitd.models import Word

FONTS = Path(__file__).resolve().parents[1] / "src" / "baitd" / "assets" / "fonts"
TEXT = ("¿Sabías que el estrés crónico daña tu memoria? Respira hondo, camina diez minutos "
        "y duerme mejor cada noche. Tu cerebro te lo agradecerá muchísimo.")


def ws(text, step=0.4):
    return [Word(t, i * step, i * step + step - 0.05) for i, t in enumerate(text.split())]


WORDS = ws(TEXT)


def read(path):
    return path.read_text(encoding="utf-8-sig")


def all_dialogues(path):
    return [l for l in read(path).splitlines() if l.startswith("Dialogue:")]


def dialogues(path):
    """Text-layer events (layer 0): one per (chunk x active word)."""
    return [l for l in all_dialogues(path) if l.startswith("Dialogue: 0,")]


def box_layer(path):
    return [l for l in all_dialogues(path) if l.startswith("Dialogue: 1,")]


def fields(line, n=10):
    return line.removeprefix("Dialogue: ").split(",", n - 1)


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def styles(path):
    out = {}
    for l in read(path).splitlines():
        if l.startswith("Style:"):
            f = l.removeprefix("Style: ").split(",")
            out[f[0]] = f
    return out


def plain(text):
    return re.sub(r"\{[^}]*\}", "", text)


def family_names():
    names = set()
    for p in FONTS.glob("*.ttf"):
        with TTFont(p) as font:
            names.add(font["name"].getDebugName(1))
    return names


def test_hex_to_ass_is_bgr_with_alpha():
    assert hex_to_ass("#0046DE") == "&H00DE4600"
    assert hex_to_ass("#FFD400") == "&H0000D4FF"
    assert hex_to_ass("ffffff", alpha=0x80) == "&H80FFFFFF"


def test_chunk_limits():
    chunks = chunk_words(WORDS)
    assert sum(len(c) for c in chunks) == len(WORDS)
    assert all(1 <= len(c) <= 4 and len(" ".join(w.text for w in c)) <= 22 for c in chunks)


def test_chunk_breaks_after_punctuation():
    assert chunk_words(ws("Hola, mundo bonito"))[0][-1].text == "Hola,"
    assert chunk_words(ws("¿Qué? Nada más"))[0][-1].text == "¿Qué?"


def test_chunk_oversized_word_stays_alone():
    chunks = chunk_words(ws("anticonstitucionalmente es largo"))
    assert [w.text for w in chunks[0]] == ["anticonstitucionalmente"]


def test_chunk_empty():
    assert chunk_words([]) == []


@pytest.mark.parametrize("text,expected", [
    ("¿Sabías que el estrés…?", True), ("Ça va très bien, œuvre", True), ("123 !", True),
    ("Zażółć gęślą jaźń", True), ("これは日本語です", False), ("Привет мир", False),
    ("مرحبا", False),
])
def test_is_latin(text, expected):
    assert is_latin(text) is expected


def test_ass_header(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(WORDS, PRESETS["caja_azul"], 6, 1080, 1920, out, FONTS)
    body = read(out)
    assert "PlayResX: 1080" in body and "PlayResY: 1920" in body
    assert "WrapStyle: 2" in body and "ScaledBorderAndShadow: yes" in body
    style = styles(out)["Caja_Text"]
    assert style[1] == "Montserrat ExtraBold"
    assert style[-4:-2] == ["90", "90"]  # MarginL, MarginR


def test_ass_has_one_event_per_word(tmp_path):
    write_ass(WORDS, PRESETS["caja_azul"], 6, 1080, 1920, tmp_path / "s.ass", FONTS)
    assert len(dialogues(tmp_path / "s.ass")) == len(WORDS)


def test_ass_timing_matches_words(tmp_path):
    write_ass(WORDS, PRESETS["caja_azul"], 6, 1080, 1920, tmp_path / "s.ass", FONTS)
    f = fields(dialogues(tmp_path / "s.ass")[0])
    assert abs(secs(f[1]) - WORDS[0].start) < 0.01
    assert secs(f[2]) > secs(f[1])


def fs_of(line):
    m = re.search(r"\\fs(\d+)", fields(line)[9])
    return int(m.group(1)) if m else None


def test_hook_is_bigger(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos tres cuatro cinco"), PRESETS["caja_azul"], 2, 1080, 1920, out, FONTS)
    d = dialogues(out)
    base = int(styles(out)["Caja_Text"][2])
    assert abs(fs_of(d[0]) / base - 1.25) < 0.02
    assert fs_of(d[-1]) is None  # body uses the style size
    assert "fscx" not in "".join(d) and "fscy" not in "".join(d)


def test_hook_is_bigger_in_every_preset(tmp_path):
    for key in PRESETS:
        out = tmp_path / f"{key}.ass"
        write_ass(ws("uno dos tres cuatro"), PRESETS[key], 2, 1080, 1920, out, FONTS)
        d = dialogues(out)
        name = "Caja_Text" if PRESETS[key].box else "Cap"
        assert abs(fs_of(d[0]) / int(styles(out)[name][2]) - 1.25) < 0.02, key
        assert fs_of(d[-1]) is None, key


def test_hook_chunks_do_not_mix_with_body(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos tres cuatro cinco seis siete ocho"), PRESETS["caja_azul"], 3, 1080, 1920, out, FONTS)
    big = [fs_of(d) is not None for d in dialogues(out)]
    assert big == [True] * 3 + [False] * 5


def test_active_word_gets_highlight_colour(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos tres"), PRESETS["amarillo_clasico"], 0, 1080, 1920, out, FONTS)
    text = [fields(x)[9] for x in dialogues(out)]
    assert "{\\1c&H00D4FF&}dos{\\1c&HFFFFFF&}" in text[1]
    assert "{\\1c&H00D4FF&}uno" in text[0] and "00D4FF" not in text[0].split("uno")[1]
    assert "{\\1c&H00D4FF&}uno" not in text[1]


def test_caja_azul_uses_two_layers(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos tres"), PRESETS["caja_azul"], 0, 1080, 1920, out, FONTS)
    text, box = dialogues(out), box_layer(out)
    assert len(text) == len(box) == 3
    st = styles(out)
    # Layer 0: legible text, dark outline, no box. Layer 1: opaque box in the highlight colour.
    assert fields(text[0])[3] == "Caja_Text" and st["Caja_Text"][15] == "1" and float(st["Caja_Text"][16]) > 0
    assert fields(box[0])[3] == "Caja_Box" and st["Caja_Box"][15] == "3" and st["Caja_Box"][5] == "&H00DE4600"
    for t, b, word in zip(text, box, ["uno", "dos", "tres"]):
        assert fields(t)[1:3] == fields(b)[1:3]  # same timing
        assert fields(t)[9].split("}")[0] in fields(b)[9]  # same position/size head
        # Both layers carry the same plain text, so libass lays the glyphs out identically.
        assert plain(fields(t)[9]) == plain(fields(b)[9]) == "uno dos tres"
        # Text layer: no per-word overrides at all. Box layer: everything hidden but the active word.
        assert "}" not in fields(t)[9].split("}", 1)[1]
        assert "\\1a&HFF&\\3a&HFF&\\4a&HFF&" in fields(b)[9]
        assert "{\\1a&H00&\\3a&H00&}" + word + "{\\1a&HFF&\\3a&HFF&}" in fields(b)[9]


def test_poster_is_uppercase(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("hola mundo"), PRESETS["poster"], 0, 1080, 1920, out, FONTS)
    assert plain(fields(dialogues(out)[0])[9]) == "HOLA MUNDO"


def test_non_latin_phrase_mode(tmp_path):
    words = [Word(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(
        ["これは", "とても", "長い", "日本語の", "文章です。", "次の", "文も", "あります。"])]
    out = tmp_path / "s.ass"
    write_ass(words, PRESETS["caja_azul"], 0, 1080, 1920, out, FONTS)
    d = dialogues(out)
    assert 1 <= len(d) < len(words)
    assert styles(out)[fields(d[0])[3]][1] == "Noto Sans"
    assert all("\\3a&H00&" not in x and "\\1c" not in x for x in d)
    assert styles(out)[fields(d[0])[3]][15] == "1"  # no box in phrase mode
    assert abs(secs(fields(d[0])[1]) - words[0].start) < 0.01
    assert any(plain(fields(x)[9]).endswith("文章です。") for x in d)  # sentence end closes a phrase


def test_every_preset_font_is_a_bundled_family():
    fams = family_names()
    assert {p.font for p in PRESETS.values()} <= fams
    assert "Noto Sans" in fams


def test_presets_values():
    p = PRESETS
    assert (p["caja_azul"].font, p["caja_azul"].highlight, p["caja_azul"].box) == (
        "Montserrat ExtraBold", "#0046DE", True)
    assert (p["amarillo_clasico"].highlight, p["amarillo_clasico"].outline) == ("#FFD400", 6)
    assert p["minimal_blanco"].font == "Inter SemiBold" and p["minimal_blanco"].size < p["caja_azul"].size
    assert (p["poster"].font, p["poster"].highlight) == ("Anton", "#0A4CF0")
    assert all(k == v.preset for k, v in p.items())


def test_default_profile_uses_caja_azul_preset():
    assert default_profile().subtitle == PRESETS["caja_azul"]
    assert default_profile().subtitle is not PRESETS["caja_azul"]


@pytest.mark.parametrize("w,h,position,expected", [
    (1080, 1920, "bottom", 1306), (1080, 1920, "middle", 960), (1080, 1920, "top", 480),
    (1920, 1080, "bottom", 918), (1920, 1080, "middle", 540), (1920, 1080, "top", 162),
])
def test_caption_baseline(w, h, position, expected):
    assert caption_baseline_y(position, w, h) == expected


def test_event_position_uses_baseline(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos"), PRESETS["minimal_blanco"], 0, 1080, 1920, out, FONTS)
    m = re.search(r"\\pos\((\d+),(\d+)\)", fields(dialogues(out)[0])[9])
    assert int(m.group(1)) == 540
    # \an2 anchors the bottom of the line box, i.e. baseline + descent.
    assert 1306 < int(m.group(2)) < 1306 + 40


def test_long_chunk_is_shrunk_to_fit_safe_width(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("Extraordinariamente increíblemente"), PRESETS["caja_azul"], 2, 1080, 1920, out, FONTS)
    m = re.search(r"\\fs(\d+)", fields(dialogues(out)[0])[9])
    assert m and int(m.group(1)) < int(styles(out)["Caja_Text"][2]) * 1.25


def test_ass_font_size_is_em_not_libass_cell_height(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno"), PRESETS["caja_azul"], 0, 1080, 1920, out, FONTS)
    assert int(styles(out)["Caja_Text"][2]) > 72  # libass Fontsize = winAscent+winDescent, larger than the em


def test_fonts_dir_constant_points_at_bundle():
    assert FONTS_DIR.is_dir() and (FONTS_DIR / "NotoSans-Regular.ttf").is_file()


def test_missing_style_font_raises(tmp_path):
    empty = tmp_path / "fonts"
    empty.mkdir()
    with pytest.raises(ValueError, match="Montserrat ExtraBold"):
        write_ass(ws("uno dos"), PRESETS["caja_azul"], 0, 1080, 1920, tmp_path / "s.ass", empty)


def test_missing_fallback_font_raises(tmp_path):
    only = tmp_path / "fonts"
    only.mkdir()
    (only / "Anton-Regular.ttf").write_bytes((FONTS / "Anton-Regular.ttf").read_bytes())
    with pytest.raises(ValueError, match="Noto Sans"):
        write_ass(ws("uno dos"), PRESETS["poster"], 0, 1080, 1920, tmp_path / "s.ass", only)


def test_hook_still_enlarged_without_metrics(tmp_path, monkeypatch):
    from baitd import captions
    monkeypatch.setattr(captions, "_faces_in", lambda _d: {"Montserrat ExtraBold": None, "Noto Sans": None})
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos tres"), PRESETS["caja_azul"], 1, 1080, 1920, out, FONTS)
    d = dialogues(out)
    assert fs_of(d[0]) == round(72 * 1.25) and fs_of(d[1]) is None


@pytest.mark.parametrize("gap,held", [(0.3, True), (0.6, False)])
def test_event_timing_with_pauses(tmp_path, gap, held):
    out = tmp_path / "s.ass"
    words = [Word("uno", 0.0, 0.2), Word("dos", 0.2 + gap, 0.2 + gap + 0.2),
             Word("tres", 0.4 + 2 * gap, 0.6 + 2 * gap)]
    write_ass(words, PRESETS["amarillo_clasico"], 0, 1080, 1920, out, FONTS)
    ev = [(secs(fields(l)[1]), secs(fields(l)[2])) for l in dialogues(out)]
    for (start, end), w in zip(ev, words):
        assert abs(start - w.start) < 0.01
    for (_, end), (nxt_start, _) in zip(ev, ev[1:]):
        assert end <= nxt_start + 0.005
    assert (abs(ev[0][1] - words[1].start) < 0.01) is held  # held through short pauses (no blink)
    if not held:
        assert abs(ev[0][1] - words[0].end) < 0.01


def test_minimal_blanco_is_legible_on_white():
    assert PRESETS["minimal_blanco"].outline == 2


def test_minimal_style_has_outline_and_shadow(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos"), PRESETS["minimal_blanco"], 0, 1080, 1920, out, FONTS)
    st = styles(out)["Cap"]
    assert float(st[16]) == 2 and float(st[17]) > 0  # Outline, Shadow


def test_caja_text_outline_is_nearly_opaque(tmp_path):
    out = tmp_path / "s.ass"
    write_ass(ws("uno dos"), PRESETS["caja_azul"], 0, 1080, 1920, out, FONTS)
    assert styles(out)["Caja_Text"][5] == "&H33000000"
