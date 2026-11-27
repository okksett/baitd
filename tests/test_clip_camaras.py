import json

from baitd.clip import camaras


def test_remember_load_forget_round_trip(tmp_data):
    assert camaras.load("Rubius") is None
    camaras.remember("@Rubius", [0, 540, 480, 270], 1920, 1080)
    assert camaras.load("rubius") == [0.0, 0.5, 0.25, 0.25]  # one key for "@Rubius", "rubius", " RUBIUS "
    assert camaras.load(" RUBIUS ") == [0.0, 0.5, 0.25, 0.25]
    camaras.remember("otro", [10, 10, 100, 50], 1280, 720)
    camaras.forget("Rubius")
    assert camaras.load("rubius") is None and camaras.load("otro") is not None
    camaras.forget("nadie")  # forgetting an unknown channel is fine
    assert set(json.loads((tmp_data / "camaras.json").read_text(encoding="utf-8"))) == {"otro"}


def test_no_channel_is_never_remembered(tmp_data):
    camaras.remember("", [0, 0, 100, 100], 1920, 1080)
    camaras.remember("@", [0, 0, 100, 100], 1920, 1080)
    assert camaras.load("") is None and not (tmp_data / "camaras.json").exists()


def test_bad_json_and_bad_entries_are_ignored(tmp_data):
    (tmp_data / "camaras.json").write_text("{no", encoding="utf-8")
    assert camaras.load("rubius") is None
    camaras.remember("rubius", [0, 0, 192, 108], 1920, 1080)  # a damaged file does not block a new box
    assert camaras.load("rubius") == [0.0, 0.0, 0.1, 0.1]
    (tmp_data / "camaras.json").write_text(json.dumps({
        "a": [0, 0, 2, 0.1], "b": "x", "c": [0, 0, 0.1], "d": [0.1, 0.1, 0.2, True], "e": [-0.1, 0, 0.1, 0.1]}),
        encoding="utf-8")
    assert all(camaras.load(k) is None for k in "abcde")


def test_scaled_fractions_to_pixels():
    box = [0.25, 0.05, 0.2, 0.2]
    assert camaras.scaled(box, 1920, 1080) == [480, 54, 384, 216]
    assert camaras.scaled(box, 1280, 720) == [320, 36, 256, 144]
    assert camaras.scaled([0.9, 0.9, 0.1, 0.1], 1921, 1081) == [1729, 973, 192, 108]  # stays inside the frame
