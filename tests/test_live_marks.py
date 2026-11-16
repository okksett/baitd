from baitd.live.marks import EXTEND_S, MAX_MARK_S, Mark, MarkTracker


def test_constants():
    assert EXTEND_S == 15.0
    assert MAX_MARK_S == 180.0


def test_press_opens_window():
    t = MarkTracker()
    m = t.press(100)
    assert (m.start, m.end, m.presses) == (55, 115, 1)
    assert t.pending() == m and t.pending() is not m


def test_presses_extend_and_cap():
    t = MarkTracker()
    t.press(100)
    m = t.press(110)
    assert m.end == 130 and m.presses == 2
    for _ in range(10):
        m = t.press(111)
    assert m.end == 55 + 180 == 235
    assert m.presses == 12


def test_press_after_end_opens_new_mark():
    t = MarkTracker()
    assert (t.press(100).start, t.press(100).end) == (55, 130)  # same-time re-press extends
    t = MarkTracker()
    t.press(100)
    m = t.press(120)
    assert (m.start, m.end, m.presses) == (75, 135, 1)
    assert (t.pending().start, t.pending().end) == (75, 135)
    first = t.ready(116)
    assert (first.start, first.end) == (55, 115)
    assert t.ready(116) is None
    second = t.ready(136)
    assert (second.start, second.end) == (75, 135)
    assert t.ready(999) is None and t.pending() is None


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
    assert t.flush() == []
    t.press(100)
    t.press(120)
    out = t.flush()
    assert [(m.start, m.end) for m in out] == [(55, 115), (75, 135)]
    assert t.pending() is None
    assert t.flush() == []


def test_earliest_start():
    t = MarkTracker()
    assert t.earliest_start() is None
    t.press(100)
    t.press(120)
    assert t.earliest_start() == 55
    t.ready(116)
    assert t.earliest_start() == 75


def test_pending_is_a_copy():
    t = MarkTracker()
    t.press(100)
    t.pending().end = 999
    t.press(100).start = 0
    assert (t.pending().start, t.pending().end) == (55, 130)


def test_clamp_start_applies_to_queued_marks():
    t = MarkTracker()
    t.press(100)
    t.press(120)
    t.clamp_start(80)
    out = t.flush()
    assert [m.start for m in out] == [80, 80]


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
