import importlib
import pathlib
import re
import subprocess

OLD = "short" + "smith"  # spelled in two parts so this file does not match itself


def test_package_is_baitd():
    assert importlib.import_module("baitd.__main__").main


def test_old_name_is_gone_from_the_repo():
    root = pathlib.Path(__file__).resolve().parents[1]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
    hits = [f for f in files if f and (re.search(OLD, f, re.I) or (
        (root / f).is_file() and re.search(OLD, (root / f).read_bytes().decode("utf-8", "ignore"), re.I)))]
    assert hits == []
