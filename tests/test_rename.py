import importlib
import pathlib
import re


def test_package_is_baitd():
    assert importlib.import_module("baitd.__main__").main


def test_no_baitd_left_in_sources():
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {"config.py", "__main__.py"}  # legacy data-dir / env-var migration only
    hits = [p for p in (root / "src").rglob("*")
            if p.is_file() and p.suffix in {".py", ".html", ".js", ".css", ".json"} and p.name not in allowed
            and re.search(r"baitd", p.read_text(encoding="utf-8"), re.I)]
    assert hits == []
