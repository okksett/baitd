"""Word-by-word captions: chunking, style presets and ASS generation."""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fontTools.ttLib import TTFont

from .models import SubtitleStyle, Word

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
FALLBACK_FONT = "Noto Sans"

PRESETS: dict[str, SubtitleStyle] = {
    "caja_azul": SubtitleStyle(
        preset="caja_azul", font="Montserrat ExtraBold", size=72, color="#FFFFFF",
        highlight="#0046DE", outline=0, box=True, position="bottom"),
    "amarillo_clasico": SubtitleStyle(
        preset="amarillo_clasico", font="Montserrat ExtraBold", size=72, color="#FFFFFF",
        highlight="#FFD400", outline=6, box=False, position="bottom"),
    "minimal_blanco": SubtitleStyle(
        preset="minimal_blanco", font="Inter SemiBold", size=56, color="#FFFFFF",
        highlight="#FFE27A", outline=0, box=False, position="bottom"),
    "poster": SubtitleStyle(
        preset="poster", font="Anton", size=96, color="#FFFFFF",
        highlight="#0A4CF0", outline=4, box=False, position="bottom"),
}
_UPPERCASE_PRESETS = {"poster"}

_BREAK_AFTER = ".,;:!?¿¡"
_PHRASE_BREAK_AFTER = _BREAK_AFTER + "。！？、，；：…"
_PHRASE_CHARS = 12
_SAFE_MARGIN = 90  # MarginL / MarginR, in output pixels
_HOOK_SCALE = 1.25
_BOX_PAD = 14  # padding of the active-word box, in output pixels
_SHADOW = 3
_BASELINE = {  # fraction of the output height where the text baseline sits
    "portrait": {"bottom": 0.68, "middle": 0.50, "top": 0.25},
    "landscape": {"bottom": 0.85, "middle": 0.50, "top": 0.15},
}


def hex_to_ass(color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS style colour; alpha 0 = opaque)."""
    h = color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _tag_color(color: str) -> str:
    """Colour for override tags: '&HBBGGRR&'."""
    return "&H" + hex_to_ass(color)[4:] + "&"


def is_latin(text: str) -> bool:
    """True when the letters in `text` are (almost) all Latin script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if unicodedata.name(c, "").startswith("LATIN"))
    return latin / len(letters) >= 0.8


def chunk_words(words: list[Word], max_words: int = 4, max_chars: int = 22) -> list[list[Word]]:
    """Group words into caption chunks; a chunk ends after sentence/clause punctuation."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur and (len(cur) >= max_words or len(" ".join(x.text for x in cur + [w])) > max_chars):
            chunks.append(cur)
            cur = []
        cur.append(w)
        if w.text and w.text[-1] in _BREAK_AFTER:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _chunk_phrases(words: list[Word]) -> list[list[Word]]:
    """Non-Latin scripts: short phrases of about 12 characters, closed by sentence punctuation."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    size = 0
    for w in words:
        n = len(w.text.strip())
        if cur and size + n > _PHRASE_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += n
        if w.text and w.text[-1] in _PHRASE_BREAK_AFTER:
            chunks.append(cur)
            cur, size = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def _joiner(words: list[Word]) -> str:
    """Scripts written without spaces are joined directly."""
    sample = "".join(w.text for w in words)
    spaceless = sum(
        1 for c in sample
        if unicodedata.name(c, "").split(" ")[0] in ("CJK", "HIRAGANA", "KATAKANA", "THAI"))
    return "" if spaceless > len(sample) / 2 else " "


def caption_baseline_y(position: str, width: int, height: int) -> int:
    """Pixel row of the text baseline for a position inside the platform safe zone."""
    key = "landscape" if width > height else "portrait"
    return round(height * _BASELINE[key][position])


# ---------- font metrics ----------

@dataclass(frozen=True)
class _Face:
    cmap: dict
    advances: dict
    upm: int
    descent: int  # font units (usWinDescent: where libass puts the baseline above the line bottom)
    notdef: int
    cell: float  # libass "font height" (winAscent + winDescent) in em

    def ass_size(self, em: float) -> int:
        """libass treats Fontsize as the cell height, so convert from an em size in pixels."""
        return round(em * self.cell)

    def width(self, text: str, size: float) -> float:
        total = 0.0
        for ch in text:
            gid = self.cmap.get(ord(ch))
            if gid is None:
                # Not in this font (e.g. CJK in Noto Sans): the system fallback draws it ~1 em wide.
                total += size if ord(ch) >= 0x2E80 else size * self.notdef / self.upm
            else:
                total += size * self.advances[gid][0] / self.upm
        return total


@lru_cache(maxsize=None)
def _load_face(path: Path) -> tuple[str, _Face]:
    with TTFont(path) as font:
        family = font["name"].getDebugName(1)
        hmtx = font["hmtx"].metrics
        cmap = font.getBestCmap()
        advances = {name: hmtx[name] for name in set(cmap.values())}
        upm = font["head"].unitsPerEm
        os2 = font["OS/2"]
        face = _Face(
            cmap=cmap, advances=advances, upm=upm, descent=os2.usWinDescent,
            notdef=hmtx[".notdef"][0] if ".notdef" in hmtx else 500,
            cell=(os2.usWinAscent + os2.usWinDescent) / upm)
    return family, face


@lru_cache(maxsize=None)
def _faces_in(fonts_dir: Path) -> dict[str, _Face]:
    out: dict[str, _Face] = {}
    for p in sorted(fonts_dir.glob("*.ttf")):
        family, face = _load_face(p)
        out.setdefault(family, face)
    return out


# ---------- ASS ----------

def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "").replace("}", "").replace("\n", " ")


def _style_line(name: str, font: str, face: _Face | None, style: SubtitleStyle, height: int,
                baseline: int, box: bool) -> str:
    size = face.ass_size(style.size) if face else style.size
    # Box look: opaque border style, whose per-glyph border is made visible on the active word only.
    border_style, outline, outline_color = (3, _BOX_PAD, style.highlight) if box else (1, style.outline, "#000000")
    shadow = _SHADOW if (not box and style.outline == 0) else 0
    return (
        f"Style: {name},{font},{size},{hex_to_ass(style.color)},{hex_to_ass(style.color)},"
        f"{hex_to_ass(outline_color)},{hex_to_ass('#000000', 0x80)},0,0,0,0,100,100,0,0,{border_style},"
        f"{outline},{shadow},2,{_SAFE_MARGIN},{_SAFE_MARGIN},{max(0, height - baseline)},1")


def write_ass(
    words: list[Word], style: SubtitleStyle, hook_word_count: int,
    width: int, height: int, path: Path, fonts_dir: Path,
) -> None:
    """Write an .ass file with one event per (chunk x active word); phrase events for non-Latin."""
    baseline = caption_baseline_y(style.position, width, height)
    latin = is_latin(" ".join(w.text for w in words))
    faces = _faces_in(Path(fonts_dir).resolve())
    upper = style.preset in _UPPERCASE_PRESETS and latin

    font_name = style.font if latin else FALLBACK_FONT
    style_name = "Cap" if latin else "CapFallback"
    face = faces.get(font_name)
    avail = width - 2 * _SAFE_MARGIN

    hook = max(0, min(hook_word_count, len(words)))
    groups = [(words[:hook], True), (words[hook:], False)]
    chunk_fn = chunk_words if latin else _chunk_phrases

    events: list[str] = []
    for group, is_hook in groups:
        chunks = chunk_fn(group)
        for chunk in chunks:
            scale = _HOOK_SCALE if is_hook else 1.0
            texts = [_clean(w.text.upper() if upper else w.text) for w in chunk]
            joiner = " " if latin else _joiner(chunk)
            line = joiner.join(texts)
            boxed = latin and style.box
            pad = 2 * _BOX_PAD if boxed else 2 * style.outline
            em = float(style.size)
            if face is not None:
                need = face.width(line, em) * scale + pad
                if need > avail:
                    em = max(1.0, math.floor(em * (avail - pad) / (need - pad)))
            descent = face.descent / face.upm * em * scale if face else 0.0
            y = round(baseline + descent)
            head = f"\\an2\\pos({width // 2},{y})"
            if boxed:
                head += "\\3a&HFF&"
            if face is not None and em != style.size:
                head += f"\\fs{face.ass_size(em)}"
            if is_hook:
                head += f"\\fscx{round(_HOOK_SCALE * 100)}\\fscy{round(_HOOK_SCALE * 100)}"
                if boxed:
                    # libass applies the vertical scale (fscy) twice to the opaque box height, so the
                    # scaled box already overshoots the text: drop its vertical padding.
                    head += "\\ybord0"

            nxt_chunk_start = chunk[-1].end
            if latin:
                for wi, w in enumerate(chunk):
                    if wi + 1 < len(chunk):
                        nxt = chunk[wi + 1].start
                        end = nxt if nxt - w.end <= 0.35 else w.end
                    else:
                        end = nxt_chunk_start
                    end = max(end, w.start + 0.04)
                    parts = []
                    for ti, t in enumerate(texts):
                        parts.append(_mark(t, style) if ti == wi else t)
                    events.append(_dialogue(w.start, end, style_name, f"{{{head}}}" + " ".join(parts)))
            else:
                end = max(chunk[-1].end, chunk[0].start + 0.04)
                events.append(_dialogue(chunk[0].start, end, style_name, f"{{{head}}}{line}"))

    header = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: None", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        _style_line("Cap", style.font, faces.get(style.font), style, height, baseline, style.box),
        _style_line("CapFallback", FALLBACK_FONT, faces.get(FALLBACK_FONT), style, height, baseline, False),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    Path(path).write_text("\n".join(header + events) + "\n", encoding="utf-8")


def _dialogue(start: float, end: float, style_name: str, text: str) -> str:
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},{style_name},,0,0,0,,{text}"


def _mark(text: str, style: SubtitleStyle) -> str:
    """Wrap the active word in override tags; the surrounding text is never touched."""
    if style.box:
        return f"{{\\3a&H00&}}{text}{{\\3a&HFF&}}"
    return f"{{\\1c{_tag_color(style.highlight)}}}{text}{{\\1c{_tag_color(style.color)}}}"
