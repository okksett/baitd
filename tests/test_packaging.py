from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"


def test_installer_script():
    iss = (PACKAGING / "baitd.iss").read_text("utf-8")
    for needle in ("PrivilegesRequired=lowest", "OutputBaseFilename=Baitd-Setup", "SetupIconFile=baitd.ico",
                   "AppVersion={#AppVersion}", "{param:UPDATE|0}", "Check: IsUpdate",
                   "function IsUpdate: Boolean"):
        assert needle in iss, needle
    run = iss.split("[Run]", 1)[1].split("[Code]", 1)[0]
    entries = [ln for ln in run.splitlines() if ln.strip().startswith("Filename:")]
    assert len(entries) == 2
    assert "postinstall skipifsilent" in entries[0]
    assert "Check: IsUpdate" in entries[1] and "postinstall" not in entries[1]


def test_spec_uses_icons():
    spec = (PACKAGING / "baitd.spec").read_text("utf-8")
    assert "baitd.ico" in spec and "baitd.icns" in spec


def test_icon_files_exist():
    for name in ("baitd.ico", "baitd.icns", "logo-source.png"):
        assert (PACKAGING / name).stat().st_size > 0, name
    assert (ROOT / "src" / "baitd" / "web" / "static" / "icon.png").stat().st_size > 0


def test_installer_does_not_restart_applications():
    # the Check: IsUpdate [Run] entry reopens Baitd; Restart Manager must not open it a second time
    assert "RestartApplications=no" in (PACKAGING / "baitd.iss").read_text("utf-8")


def test_spec_collects_live_dependencies():
    spec = (PACKAGING / "baitd.spec").read_text("utf-8")
    assert 'collect_submodules("aiohttp")' in spec
    assert 'collect_submodules("pynput")' in spec
    # pynput is only needed (and only installed) away from Windows
    assert 'sys.platform != "win32"' in spec.split('collect_submodules("pynput")')[0].rsplit("\n", 3)[-3] + spec


def test_pyproject_lists_live_dependencies():
    text = (ROOT / "pyproject.toml").read_text("utf-8")
    deps = text.split("dependencies = [", 1)[1].split("]", 1)[0]
    assert '"aiohttp>=3.9,<4"' in deps
    assert "pynput" in deps and "sys_platform != 'win32'" in deps.split("pynput", 1)[1].split("\n", 1)[0]
