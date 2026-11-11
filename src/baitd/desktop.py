"""Baitd in its own window instead of a browser tab.

pywebview shows the local server's page in a native window: Edge WebView2 on Windows (part of Windows
10 and 11), WebKit on macOS. Closing the window quits Baitd. Where no such window can be made (pywebview
missing, Windows without WebView2, Linux without GTK/Qt) run_window() returns False and the caller opens
the browser as before. pywebview would fall back to Internet Explorer's engine on a Windows without
WebView2; that engine cannot run the interface, so it counts as "no window".
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

TITLE = "Baitd"
SIZE = (1280, 860)
MIN_SIZE = (960, 640)
BACKGROUND = "#0b1020"  # the app's dark background, shown while the page loads

_window = None
_lock = threading.Lock()


def run_window(url: str, storage: Path) -> bool:
    """Show `url` in a native window and block until the user closes it (main thread only).
    `storage` keeps the page's localStorage between runs. False when no window can be made here."""
    global _window
    # debug=True below is only for the right-click menu (Copiar/Pegar in the link field); keep
    # pywebview's own log at warnings, which debug would otherwise raise to every detail
    os.environ.setdefault("PYWEBVIEW_LOG", "WARNING")
    try:
        import webview
    except Exception as e:  # not installed, or its native parts are missing
        log.info("Sin ventana propia (pywebview no disponible: %s); se usa el navegador", e)
        return False
    if sys.platform == "win32" and not _has_webview2():
        log.info("Sin ventana propia (falta WebView2 de Microsoft Edge); se usa el navegador")
        return False
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False  # debug only to keep the right-click menu
    try:
        window = webview.create_window(TITLE, url, width=SIZE[0], height=SIZE[1], min_size=MIN_SIZE,
                                       background_color=BACKGROUND, text_select=True)
        with _lock:
            _window = window
        storage.mkdir(parents=True, exist_ok=True)
        # pywebview turns the right-click menu off unless debug: without it nobody could right-click
        # → Pegar a link. Devtools stay closed (OPEN_DEVTOOLS_IN_DEBUG) and only "Inspeccionar" shows.
        webview.start(debug=True, private_mode=False, storage_path=str(storage))
    except Exception as e:  # e.g. Linux without GTK or Qt
        log.warning("No se pudo abrir la ventana de Baitd (%s); se usa el navegador", e)
        return False
    finally:
        with _lock:
            _window = None
    return True


def _has_webview2() -> bool:
    try:
        from webview.platforms import winforms
        return winforms.renderer == "edgechromium"
    except Exception:
        return False


def close_window() -> None:
    """Close the window from any thread (Salir, the updater); run_window() then returns."""
    with _lock:
        window = _window
    if window is not None:
        try:
            window.destroy()
        except Exception:  # already closing
            log.debug("close_window", exc_info=True)


def focus_window() -> bool:
    """Bring the window to the front (Baitd was opened a second time). False when there is no window."""
    with _lock:
        window = _window
    if window is None:
        return False
    try:
        window.restore()
        window.show()
        window.on_top = True   # Windows does not let a background process just take the focus
        window.on_top = False
    except Exception:
        log.debug("focus_window", exc_info=True)
    return True
