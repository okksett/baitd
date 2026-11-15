"""The global hotkey that marks a moment while a game has focus.

Windows registers it with `RegisterHotKey` on a daemon thread that runs the message loop (the call is
thread-affine: the key is bound to the thread that registered it, and that thread receives `WM_HOTKEY`).
macOS and Linux use pynput's `GlobalHotKeys` when it imports. Nothing else here is platform specific, and
`ctypes.windll` is only touched when a hotkey starts, so importing this module works everywhere.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from typing import Callable

from ..models import UserError

__all__ = ["HotkeyError", "HotkeyListener", "parse_hotkey", "format_hotkey"]

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
HOTKEY_ID = 1
START_TIMEOUT_S = 3.0
STOP_TIMEOUT_S = 2.0

_MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_MOD_ORDER = ("ctrl", "alt", "shift", "win")
_MOD_LABEL = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
_NAMED_VK = {"pause": 0x13, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24, "insert": 0x2D}
_NAMED_LABEL = {"pause": "Pause", "insert": "Insert", "home": "Home", "end": "End",
                "pageup": "PageUp", "pagedown": "PageDown"}
_PYNPUT_MOD = {"ctrl": "<ctrl>", "alt": "<alt>", "shift": "<shift>", "win": "<cmd>"}
_PYNPUT_NAMED = {"pageup": "<page_up>", "pagedown": "<page_down>"}


class HotkeyError(UserError):
    """The key cannot be used, or the system refused it."""


def _bad() -> HotkeyError:
    return HotkeyError("Esa tecla no se puede usar",
                       "Elige una tecla de función (F1–F24), o una letra o un número con Ctrl, Alt o Win")


def _valid_key(key: str) -> bool:
    if key in _NAMED_VK:
        return True
    if len(key) == 1:
        return "a" <= key <= "z" or "0" <= key <= "9"  # ASCII only: no "ñ", no other digits
    return key[0] == "f" and key[1:].isascii() and key[1:].isdigit() and 1 <= int(key[1:]) <= 24 \
        and key[1] != "0"


def parse_hotkey(spec: str) -> tuple[frozenset[str], str]:
    """`"ctrl+shift+f9"` -> `(frozenset({"ctrl", "shift"}), "f9")`: lower case, modifiers apart from the key."""
    if not isinstance(spec, str):
        raise _bad()
    parts = [p.strip().lower() for p in spec.split("+")]
    if not parts or not all(parts):
        raise _bad()
    *mods, key = parts
    if len(set(mods)) != len(mods) or any(m not in _MODS for m in mods) or not _valid_key(key):
        raise _bad()
    # a letter or digit with no modifier, or with Shift only (capitals, symbols), would steal typing
    # system-wide: it needs Ctrl, Alt or Win (Shift may come on top)
    if len(key) == 1 and not set(mods) - {"shift"}:
        raise _bad()
    return frozenset(mods), key


def format_hotkey(mods, key: str) -> str:
    """The canonical form, for example `"Ctrl+Shift+F9"`."""
    label = _NAMED_LABEL.get(key, key.upper())
    return "+".join([_MOD_LABEL[m] for m in _MOD_ORDER if m in mods] + [label])


def _vk(key: str) -> int:
    if key in _NAMED_VK:
        return _NAMED_VK[key]
    if len(key) == 1:
        return ord(key.upper())  # '0'-'9' and 'A'-'Z' are their own virtual-key codes
    return 0x70 + int(key[1:]) - 1  # VK_F1


def _pynput_spec(mods, key: str) -> str:
    named = _PYNPUT_NAMED.get(key) or (f"<{key}>" if key in _NAMED_VK or len(key) > 1 else key)
    return "+".join([_PYNPUT_MOD[m] for m in _MOD_ORDER if m in mods] + [named])


_USER32 = None


def _user32():
    """user32 with typed signatures (a private copy: other code's use of `windll.user32` is untouched)."""
    global _USER32
    if _USER32 is None:
        from ctypes import wintypes
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        dll.RegisterHotKey.restype = wintypes.BOOL
        dll.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        dll.UnregisterHotKey.restype = wintypes.BOOL
        dll.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        dll.GetMessageW.restype = ctypes.c_int  # 0 = WM_QUIT, -1 = error
        dll.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                     wintypes.UINT]
        dll.PeekMessageW.restype = wintypes.BOOL
        dll.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        dll.PostThreadMessageW.restype = wintypes.BOOL
        _USER32 = dll
    return _USER32


class HotkeyListener:
    """Calls `on_press` (on the listener's own thread: keep it quick) each time the key is pressed.
    Holding the key down counts as one press."""

    def __init__(self, spec: str, on_press: Callable[[], None]):
        self._mods, self._key = parse_hotkey(spec)  # a bad spec fails here, before anything is registered
        self.spec = format_hotkey(self._mods, self._key)
        self._on_press = on_press
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._pynput = None

    def start(self) -> None:
        if self._thread is not None or self._pynput is not None:
            return
        if _IS_WINDOWS:
            self._start_windows()
        else:
            self._start_pynput()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        if thread is not None:
            try:
                _user32().PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
            except Exception:
                log.exception("No se pudo avisar al hilo de la tecla rápida")
            if thread is not threading.current_thread():
                thread.join(STOP_TIMEOUT_S)
        listener, self._pynput = self._pynput, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                log.exception("No se pudo soltar la tecla rápida")

    # ---- Windows ------------------------------------------------------------------

    def _start_windows(self) -> None:
        ready, cancel = threading.Event(), threading.Event()
        outcome: list[BaseException | None] = []
        thread = threading.Thread(target=self._message_loop, args=(ready, cancel, outcome), name="live-hotkey",
                                  daemon=True)
        self._thread = thread  # stored first: a late thread can still be stopped
        thread.start()
        if not ready.wait(START_TIMEOUT_S):
            cancel.set()  # the thread skips registering, or unregisters and ends if it was already past that
            self._thread = None
            if self._tid:
                try:
                    _user32().PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
                except Exception:
                    log.exception("No se pudo avisar al hilo de la tecla rápida")
            raise HotkeyError("No se pudo activar la tecla rápida", "Usa el botón Marcar de la ventana")
        if outcome[0] is not None:
            self._thread = None
            thread.join(STOP_TIMEOUT_S)
            raise outcome[0]  # type: ignore[misc]

    def _message_loop(self, ready: threading.Event, cancel: threading.Event, outcome: list) -> None:
        from ctypes import wintypes
        registered = False
        try:
            user32 = _user32()
            self._tid = threading.get_native_id()  # on Windows this is GetCurrentThreadId()
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # creates this thread's message queue
            mods = sum(_MODS[m] for m in self._mods) | MOD_NOREPEAT
            if cancel.is_set():  # start() gave up waiting
                outcome.append(HotkeyError("No se pudo activar la tecla rápida", "Usa el botón Marcar de la ventana"))
                return
            if not user32.RegisterHotKey(None, HOTKEY_ID, mods, _vk(self._key)):
                outcome.append(HotkeyError(f"La tecla {self.spec} ya la usa otro programa", "Elige otra en Ajustes"))
                return
            registered = True
            if cancel.is_set():  # gave up while RegisterHotKey ran: release it and end
                user32.UnregisterHotKey(None, HOTKEY_ID)
                return
            outcome.append(None)
        except Exception:
            log.exception("No se pudo registrar la tecla rápida")
            outcome.append(HotkeyError("No se pudo activar la tecla rápida", "Usa el botón Marcar de la ventana"))
            return
        finally:
            ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self._on_press()
                    except Exception:
                        log.exception("La tecla rápida falló")
        finally:
            if registered:
                user32.UnregisterHotKey(None, HOTKEY_ID)  # from the registering thread

    # ---- macOS and Linux ------------------------------------------------------------

    def _start_pynput(self) -> None:
        try:
            from pynput import keyboard
            listener = keyboard.GlobalHotKeys({_pynput_spec(self._mods, self._key): self._on_press})
            listener.start()
        except Exception as e:  # not installed, Wayland, or Accessibility refused
            log.warning("No se pudo activar la tecla rápida (%s)", type(e).__name__)
            raise HotkeyError("No se pudo activar la tecla rápida", "Usa el botón Marcar de la ventana") from e
        self._pynput = listener
