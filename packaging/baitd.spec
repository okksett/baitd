# PyInstaller spec: one-folder build named "Baitd" (run from the repo root:
#   pyinstaller packaging/baitd.spec).
# Windows and Linux keep a console window (the URL and "Pulsa Ctrl+C para cerrar" are shown there);
# the macOS .app is windowed and quits from the Settings dialog ("Salir de Baitd").
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PKG = ROOT / "src" / "baitd"

# Every resource is looked up relative to its module's __file__, so each folder keeps its
# "baitd/..." location inside the bundle.
datas = [
    (str(PKG / "assets"), "baitd/assets"),              # fonts (+ LICENSES) and music (+ CREDITS.md)
    (str(PKG / "web" / "templates"), "baitd/web/templates"),
    (str(PKG / "web" / "static"), "baitd/web/static"),  # css, js, i18n
]
datas += collect_data_files("imageio_ffmpeg")                # binaries/ffmpeg-* (keeps its exec bit)
datas += collect_data_files("certifi")                       # cacert.pem for HTTPS (requests, edge-tts)
datas += collect_data_files("edge_tts")

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=collect_submodules("baitd"),
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Baitd",
    console=sys.platform != "darwin",
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Baitd", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Baitd.app",
        bundle_identifier="com.baitd.app",
        info_plist={"CFBundleName": "Baitd", "CFBundleDisplayName": "Baitd",
                    "NSHighResolutionCapable": True},
    )
