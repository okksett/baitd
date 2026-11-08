import json
from pathlib import Path

from baitd.clip.models import FRAMINGS, KINDS, TRANSCRIBERS
from baitd.clip.pipeline import CLIP_JOB_KINDS, CLIP_STEPS
from baitd.web import server

I18N = Path(server.__file__).resolve().parent / "static" / "i18n"


def _load(lang: str) -> dict:
    return json.loads((I18N / f"{lang}.json").read_text("utf-8"))


def test_i18n_key_parity():
    es, en = _load("es"), _load("en")
    assert set(es) == set(en), set(es) ^ set(en)
    assert all(isinstance(v, str) and v.strip() for v in [*es.values(), *en.values()])


def test_clipping_dynamic_keys_exist():
    # app.js builds these keys from job steps/kinds and the ClipProfile choices
    es = _load("es")
    keys = ({f"step.{s}" for s in CLIP_STEPS} | {f"kind.{k}" for k in CLIP_JOB_KINDS}
            | {f"clip.kind.{k}" for k in ("todos", *KINDS)} | {f"framing.{f}" for f in FRAMINGS}
            | {f"transcriber.{t}" for t in TRANSCRIBERS})
    assert keys <= set(es), keys - set(es)


def test_rights_notice_copy():
    assert _load("es")["clip.rights"] == ("Si usas videos de otros canales, respeta sus derechos "
                                         "y las reglas de cada plataforma.")
    assert _load("en")["clip.rights"] == "If you use other channels' videos, respect their rights and each platform's rules."
