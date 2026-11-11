# PyInstaller spec: one-folder build named "Baitd" (run from the repo root:
#   pyinstaller packaging/baitd.spec).
# Windows and Linux keep a console window (the URL and "Pulsa Ctrl+C para cerrar" are shown there);
# the macOS .app is windowed and quits from the Settings dialog ("Salir de Baitd").
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

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
datas += collect_data_files("faster_whisper")                # VAD asset (silero), no Whisper model
# The YuNet face model + its licence ride along inside baitd/assets (copied above); no Whisper model is bundled.

# Clipping: yt-dlp extractors are imported dynamically, ctranslate2 ships native libs.
hiddenimports = (collect_submodules("baitd") + collect_submodules("yt_dlp") + collect_submodules("faster_whisper")
                 + collect_submodules("yt_dlp_ejs"))
binaries = collect_dynamic_libs("ctranslate2")
# YouTube needs a JavaScript runtime for its challenges: Deno (from the `deno` wheel) goes in
# _MEIPASS/deno/, where ytdlp_loader.deno_path() looks; yt-dlp's solver scripts are package data.
import deno  # noqa: E402

binaries += [(deno.find_deno_bin(), "deno")]
datas += collect_data_files("yt_dlp_ejs")

a = Analysis(
    [str(ROOT / "packaging" / "launch.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "torch", "torchvision", "torchaudio",
              "PyQt5", "PyQt6", "PySide2", "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Baitd",
    console=sys.platform != "darwin",
    icon=str(ROOT / "packaging" / "baitd.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Baitd", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Baitd.app",
        icon=str(ROOT / "packaging" / "baitd.icns"),
        bundle_identifier="com.baitd.app",
        info_plist={"CFBundleName": "Baitd", "CFBundleDisplayName": "Baitd",
                    "NSHighResolutionCapable": True},
    )
