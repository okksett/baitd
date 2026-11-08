from pathlib import Path

import pytest
from fontTools.ttLib import TTFont

FONTS = Path(__file__).resolve().parents[1] / "src" / "baitd" / "assets" / "fonts"
EXPECTED = {
    "Anton-Regular.ttf", "Inter-SemiBold.ttf", "Inter-Regular.ttf",
    "InstrumentSerif-Italic.ttf", "Montserrat-ExtraBold.ttf", "NotoSans-Regular.ttf",
}
ASCII = {chr(c) for c in range(0x20, 0x7F)}
LATIN1_LETTERS = {chr(c) for c in range(0xC0, 0x100)} - {"×", "÷"}
PROSE = set(" «»¿¡…‘’“”–—")
LATIN_EXT_A = {chr(c) for c in range(0x100, 0x180)} - {chr(0x149)}  # U+0149 is deprecated
REQUIRED = ASCII | LATIN1_LETTERS | PROSE
# Instrument Serif Italic (not used by any preset) lacks part of Latin Extended-A upstream.
NO_EXT_A = {"InstrumentSerif-Italic.ttf"}


def test_expected_fonts_are_bundled():
    assert {p.name for p in FONTS.glob("*.ttf")} == EXPECTED


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_font_is_static_and_covers_latin(name):
    with TTFont(FONTS / name) as font:
        assert "fvar" not in font, f"{name} is a variable font"
        cmap = font.getBestCmap()
    required = REQUIRED if name in NO_EXT_A else REQUIRED | LATIN_EXT_A
    missing = sorted(c for c in required if ord(c) not in cmap)
    assert not missing, missing


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_font_has_license(name):
    stem = name.split("-")[0]
    assert (FONTS / "LICENSES" / f"{stem}-OFL.txt").is_file()
    assert (FONTS / "LICENSES" / "SOURCES.md").is_file()
