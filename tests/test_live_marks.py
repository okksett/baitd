from baitd.live.marks import EXTEND_S, MAX_MARK_S, Mark, MarkTracker


def test_constants():
    assert EXTEND_S == 15.0
    assert MAX_MARK_S == 180.0


def test_press_opens_window():
    t = MarkTracker()
    m = t.press(100)
    assert (m.start, m.end, m.presses) == (55, 115, 1)
    assert t.pending() is m


def test_presses_extend_and_cap():
    t = MarkTracker()
    t.press(100)
    m = t.press(110)
    assert m.end == 130 and m.presses == 2
    for _ in range(10):
        m = t.press(111)
    assert m.end == 55 + 180 == 235
    assert m.presses == 12


def test_press_after_end_does_not_extend():
    t = MarkTracker()
    t.press(100)
    m = t.press(116)
    assert (m.end, m.presses) == (115, 1)


def test_ready_and_new_mark():
    t = MarkTracker()
    t.press(100)
    assert t.ready(None) is None
    assert t.ready(114) is None
    m = t.ready(115)
    assert isinstance(m, Mark) and (m.start, m.end) == (55, 115)
    assert t.pending() is None
    assert t.ready(999) is None
    m2 = t.press(200)
    assert (m2.start, m2.end) == (155, 215)


def test_flush():
    t = MarkTracker()
    assert t.flush() is None
    t.press(100)
    m = t.flush()
    assert (m.start, m.end) == (55, 115)
    assert t.pending() is None
    assert t.flush() is None


def test_clamp_start():
    t = MarkTracker()
    t.press(100)
    t.clamp_start(None)
    assert t.pending().start == 55
    t.clamp_start(40)
    assert t.pending().start == 55
    t.clamp_start(80)
    assert t.pending().start == 80
    empty = MarkTracker()
    empty.clamp_start(80)  # no pending: no-op
    assert empty.pending() is None


def test_custom_window():
    t = MarkTracker(before_s=10, after_s=5)
    m = t.press(100)
    assert (m.start, m.end) == (90, 105)
