import sys
import threading

import pytest

from baitd.live import channels, hotkey
from baitd.live.channels import Channel, LiveStatus
from baitd.live.hotkey import HotkeyError, format_hotkey, parse_hotkey
from baitd.live.session import LiveManager
from test_live_session import FakeCapture, SETTINGS


def test_parse_and_format():
    mods, key = parse_hotkey("ctrl+shift+f9")
    assert format_hotkey(mods, key) == "Ctrl+Shift+F9"
    assert parse_hotkey("F9") == (frozenset(), "f9")
    assert format_hotkey(*parse_hotkey(" ALT + win + PageUp ")) == "Alt+Win+PageUp"
    assert format_hotkey(*parse_hotkey("shift+ctrl+a")) == "Ctrl+Shift+A"
    assert parse_hotkey("f24")[1] == "f24" and parse_hotkey("0")[1] == "0"
    for bad in ("", "ctrl", "f25", "ctrl+ñ", "f0", "ctrl+f9+f10", "ctrl++f9", "f9+ctrl", "ctrl+ctrl+f9", None, 9):
        with pytest.raises(HotkeyError) as e:
            parse_hotkey(bad)
        assert e.value.cause == "Esa tecla no se puede usar"
        assert "Elige una tecla de función (F1–F24)" in e.value.fix


class FakeUser32:
    """Records RegisterHotKey; GetMessageW ends the loop at once (or after `hotkeys` WM_HOTKEYs)."""

    def __init__(self, register_result=1, hotkeys=0):
        self.register_result = register_result
        self.hotkeys = hotkeys
        self.registered = []
        self.unregistered = []
        self.posted = []
        self.threads = []

    def PeekMessageW(self, *a):
        return 0

    def RegisterHotKey(self, hwnd, ident, mods, vk):
        self.registered.append((hwnd, ident, mods, vk))
        self.threads.append(threading.get_ident())
        return self.register_result

    def GetMessageW(self, msg_ref, *a):
        if self.hotkeys:
            self.hotkeys -= 1
            msg_ref._obj.message = hotkey.WM_HOTKEY  # byref(msg) keeps the MSG in _obj
            return 1
        self.threads.append(threading.get_ident())
        return 0

    def UnregisterHotKey(self, hwnd, ident):
        self.unregistered.append((hwnd, ident))
        self.threads.append(threading.get_ident())
        return 1

    def PostThreadMessageW(self, tid, msg, w, l):
        self.posted.append((tid, msg))
        return 1


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey is Windows only")


@windows_only
def test_register_uses_norepeat(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(hotkey, "_user32", lambda: fake)
    listener = hotkey.HotkeyListener("F9", lambda: None)
    listener.start()
    listener.stop()
    (_, _, mods, vk), = fake.registered
    assert mods & 0x4000 and vk == 0x78
    assert mods == 0x4000  # no other modifier
    assert fake.unregistered == [(None, 1)]
    assert len(set(fake.threads)) == 1  # register, loop and unregister on the one message thread


@windows_only
def test_modifiers_map_to_win32_flags(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(hotkey, "_user32", lambda: fake)
    hotkey.HotkeyListener("ctrl+alt+shift+win+a", lambda: None).start()
    assert fake.registered[0][2] == 2 | 1 | 4 | 8 | 0x4000 and fake.registered[0][3] == 0x41


@windows_only
def test_wm_hotkey_calls_on_press(monkeypatch):
    fake = FakeUser32(hotkeys=2)
    monkeypatch.setattr(hotkey, "_user32", lambda: fake)
    got = threading.Event()
    n = []

    def pressed():
        n.append(1)
        if len(n) == 2:
            got.set()

    listener = hotkey.HotkeyListener("F9", pressed)
    listener.start()
    assert got.wait(5)
    listener.stop()
    assert len(n) == 2


@windows_only
def test_busy_key_raises(monkeypatch):
    fake = FakeUser32(register_result=0)
    monkeypatch.setattr(hotkey, "_user32", lambda: fake)
    with pytest.raises(HotkeyError) as e:
        hotkey.HotkeyListener("F9", lambda: None).start()
    assert "ya la usa otro programa" in str(e.value) and "F9" in e.value.cause
    assert e.value.fix == "Elige otra en Ajustes"
    assert fake.unregistered == []  # nothing to release


def test_bad_spec_fails_at_construction():
    with pytest.raises(HotkeyError):
        hotkey.HotkeyListener("ctrl", lambda: None)


def test_pynput_failure_is_explained(monkeypatch):
    monkeypatch.setattr(hotkey, "_IS_WINDOWS", False)
    monkeypatch.setitem(sys.modules, "pynput", None)  # import fails
    monkeypatch.setitem(sys.modules, "pynput.keyboard", None)
    with pytest.raises(HotkeyError) as e:
        hotkey.HotkeyListener("F9", lambda: None).start()
    assert e.value.cause == "No se pudo activar la tecla rápida"
    assert e.value.fix == "Usa el botón Marcar de la ventana"


def test_pynput_spec_translation():
    assert hotkey._pynput_spec(*parse_hotkey("ctrl+shift+f9")) == "<ctrl>+<shift>+<f9>"
    assert hotkey._pynput_spec(*parse_hotkey("win+pagedown")) == "<cmd>+<page_down>"
    assert hotkey._pynput_spec(*parse_hotkey("a")) == "a"


class FakeListener:
    made: list = []
    fail_with = None

    def __init__(self, spec, on_press):
        self.spec, self.on_press = spec, on_press
        self.started = self.stopped = 0
        FakeListener.made.append(self)

    def start(self):
        if FakeListener.fail_with is not None:
            raise FakeListener.fail_with
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture
def lm(tmp_data):
    FakeListener.made = []
    FakeListener.fail_with = None
    FakeCapture.made = []
    settings = dict(SETTINGS, live_hotkey="F9")
    m = LiveManager(tmp_data, lambda: settings, capture_factory=FakeCapture, hotkey_factory=FakeListener,
                    tick_s=0.05, status_fn=lambda ch: LiveStatus(False))
    m.settings = settings
    yield m
    m.close()


def _chan(i="a"):
    return Channel(id=i * 12, platform="kick", handle=f"c{i}", url=f"https://kick.com/c{i}")


def test_manager_registers_only_with_channels(lm):
    lm.channels_changed()
    assert FakeListener.made == []  # no channels: never started
    channels.save_channels([_chan("a")])
    lm.channels_changed()
    (fl,) = FakeListener.made
    assert fl.started == 1 and fl.stopped == 0 and fl.spec == "F9"
    channels.save_channels([_chan("a"), _chan("b")])
    lm.channels_changed()  # nothing to change: no second registration
    assert len(FakeListener.made) == 1 and fl.started == 1
    channels.save_channels([])
    lm.channels_changed()
    assert fl.stopped == 1
    channels.save_channels([_chan("c")])
    lm.channels_changed()  # back: registers anew
    assert len(FakeListener.made) == 2 and FakeListener.made[1].started == 1


def test_reload_hotkey_after_a_setting_change(lm):
    channels.save_channels([_chan("a")])
    lm.reload_hotkey()
    first = FakeListener.made[0]
    lm.settings["live_hotkey"] = "Ctrl+F10"
    lm.reload_hotkey()
    assert first.stopped == 1
    assert FakeListener.made[1].spec == "Ctrl+F10" and FakeListener.made[1].started == 1


def test_press_marks_with_origin_hotkey(lm):
    channels.save_channels([_chan("a")])
    lm.reload_hotkey()
    FakeListener.made[0].on_press()  # no capture: the window explains it
    assert lm.status()["events"][-1]["kind"] == "no_capture"
    lm.start(_chan("a").id)
    FakeListener.made[0].on_press()
    assert lm.status()["events"][-1]["kind"] != "no_capture"


def test_busy_hotkey_becomes_an_event(lm):
    channels.save_channels([_chan("a")])
    FakeListener.fail_with = HotkeyError("La tecla F9 ya la usa otro programa", "Elige otra en Ajustes")
    lm.reload_hotkey()
    ev = lm.status()["events"][-1]
    assert ev["kind"] == "hotkey_busy" and "ya la usa otro programa" in ev["text"]
    FakeListener.fail_with = None
    lm.reload_hotkey()  # retried on the next reload
    assert FakeListener.made[-1].started == 1


def test_close_stops_the_hotkey(lm):
    channels.save_channels([_chan("a")])
    lm.reload_hotkey()
    lm.close()
    assert FakeListener.made[0].stopped == 1
    lm.reload_hotkey()  # closed: never registers again
    assert len(FakeListener.made) == 1


def test_no_factory_no_registration(tmp_data):
    channels.save_channels([_chan("a")])
    m = LiveManager(tmp_data, lambda: dict(SETTINGS), capture_factory=FakeCapture,
                    status_fn=lambda ch: LiveStatus(False))
    m.channels_changed()
    assert m._hotkey is None
    m.close()
