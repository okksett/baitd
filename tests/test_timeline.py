import pytest

from baitd.models import Media, Word
from baitd.timeline import assign_media, plan_cuts

SCENE_TEXTS = [
    "¿Sufres de estrés? Respira hondo y cuenta hasta cuatro, luego suelta el aire despacio y con calma.",
    "Tu cuerpo responde enseguida. El pulso baja, los hombros se aflojan y la mente deja de correr tanto "
    "como antes. Es ciencia, no magia.",
    "Pero hay truco.",  # scene shorter than min_len
    "Hazlo tres veces seguidas cada mañana, antes de mirar el móvil, y notarás la diferencia durante todo "
    "el día en el trabajo.",
    "Si te cuesta al principio, no pasa nada. Empieza con un minuto y ve subiendo poco a poco cada semana "
    "hasta llegar a cinco.",
    "Añade música suave o sonidos de lluvia. Ayuda a mantener el ritmo y a no distraerte.",
    "Pruébalo hoy mismo y cuéntame qué tal te ha ido. Tu calma te lo agradecerá. ¡Hasta mañana!",
]
SCENE_LENS = [6.0, 9.5, 1.6, 8.0, 7.4, 5.5, 7.0]


def _build():
    bounds, words, t = [], [], 0.0
    for text, length in zip(SCENE_TEXTS, SCENE_LENS):
        toks = text.split()
        step = length / len(toks)
        for i, tok in enumerate(toks):
            words.append(Word(tok, t + i * step, t + (i + 1) * step))
        bounds.append((t, t + length))
        t += length
    return bounds, words


BOUNDS_45S, WORDS_45S = _build()


def _media(n_per_scene):
    return {
        s: [Media(f"m{s}-{i}", "pexels", "video", "u", 1920, 1080, 10.0) for i in range(n)]
        for s, n in n_per_scene.items()
    }


def test_fixture_is_45s():
    assert BOUNDS_45S[-1][1] == pytest.approx(45.0)


def test_all_shots_within_bounds():
    cuts = plan_cuts(BOUNDS_45S, WORDS_45S)
    assert all(2.0 <= e - s <= 4.0 + 1e-9 for _, s, e in cuts[1:-1])
    assert cuts[-1][2] - cuts[-1][1] <= 4.0 + 1e-9


def test_first_shot_is_hook():
    first = plan_cuts(BOUNDS_45S, WORDS_45S)[0]
    assert 1.0 <= first[2] <= 2.5
    # hook sentence "¿Sufres de estrés?" is the first 3 words of scene 0
    assert first[2] == pytest.approx(WORDS_45S[2].end)


def test_cuts_contiguous_and_cover():
    c = plan_cuts(BOUNDS_45S, WORDS_45S)
    assert c[0][1] == 0 and c[-1][2] == pytest.approx(45.0)
    assert all(a[2] == b[1] for a, b in zip(c, c[1:]))


def test_cuts_snap_to_word_ends():
    ends = [w.end for w in WORDS_45S]
    c = plan_cuts(BOUNDS_45S, WORDS_45S)
    inner = [e for _, _, e in c[:-1]]
    snapped = [e for e in inner if any(abs(e - x) < 1e-9 for x in ends)]
    assert len(snapped) >= len(inner) * 0.6


def test_shots_do_not_cross_scene_bounds_except_short_scene():
    cuts = plan_cuts(BOUNDS_45S, WORDS_45S)
    # scene 2 (1.6 s) merges into scene 3's first shot; every other bound is a cut
    for i, (_, end) in enumerate(BOUNDS_45S[:-1]):
        if i == 2:
            assert not any(abs(e - end) < 1e-9 for _, _, e in cuts)
        else:
            assert any(abs(e - end) < 1e-9 for _, _, e in cuts), f"bound {i}"
    merged = [c for c in cuts if c[1] == pytest.approx(BOUNDS_45S[2][0])]
    assert merged and merged[0][0] == 2


def test_short_last_scene_merges_into_previous_shot():
    bounds = [(0.0, 8.0), (8.0, 9.2)]
    words = [Word(f"w{i}", i * 0.5, i * 0.5 + 0.5) for i in range(18)]
    cuts = plan_cuts(bounds, words)
    assert cuts[-1][2] == pytest.approx(9.2)
    assert all(e - s <= 4.0 + 1e-9 for _, s, e in cuts[1:])
    assert all(e - s >= 2.0 - 1e-9 for _, s, e in cuts[1:])


def test_assign_media_no_repeats_and_zoom_alternates():
    cuts = plan_cuts(BOUNDS_45S, WORDS_45S)
    media = _media({s: 4 for s in range(7)})
    shots = assign_media(cuts, media)
    ids = [s.media.id for s in shots]
    assert len(ids) == len(set(ids)) == len(cuts)
    assert [(s.start, s.end) for s in shots] == [(s, e) for _, s, e in cuts]
    for i, s in enumerate(shots):
        assert (s.zoom_from, s.zoom_to) == ((1.0, 1.08) if i % 2 == 0 else (1.08, 1.0))


def test_assign_media_borrows_from_nearest_scene_when_exhausted():
    cuts = [(0, 0, 3.0), (0, 3.0, 6.0), (1, 6.0, 9.0)]
    media = _media({0: 1, 1: 1, 2: 1})
    shots = assign_media(cuts, media)
    # shot 1 borrows from the nearest other scene (1); shot 2's own list is then empty
    # so it borrows from scene 2 (scene 0 is exhausted)
    assert [s.media.id for s in shots] == ["m0-0", "m1-0", "m2-0"]


def test_assign_media_raises_when_not_enough():
    with pytest.raises(ValueError, match="not enough media"):
        assign_media([(0, 0, 3.0), (0, 3.0, 6.0)], _media({0: 1}))
