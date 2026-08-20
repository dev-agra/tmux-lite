"""Unit tests for CompletionObserver's decision logic in isolation.

These fake out the two things that would otherwise require a real PTY
and real wall-clock time: TIOCGPGRP polling (monkeypatched) and
time.monotonic (monkeypatched via a controllable clock). This lets us
assert on the actual decision logic -- threshold, focus suppression,
marker correlation, exactly-one-notification -- deterministically and
fast. Real-PTY, real-shell, real-timing end-to-end behaviour is covered
separately in tests/test_notifier_integration.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tmuxlite import notifier as notifier_mod
from tmuxlite.notifier import CompletionObserver, MARKER_PREFIX


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakePane:
    def __init__(self, pane_id, pid=100):
        self.pane_id = pane_id
        self.pid = pid
        self.master_fd = 1000 + pane_id  # arbitrary, unique
        self.alive = True


def make_observer(monkeypatch, clock, pgrp_by_fd, notify_fd):
    monkeypatch.setattr(notifier_mod.time, "monotonic", clock)
    monkeypatch.setattr(
        notifier_mod, "_get_foreground_pgrp",
        lambda fd: pgrp_by_fd.get(fd),
    )
    return CompletionObserver(notify_fd=notify_fd, desktop_notify=False)


def read_notify(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 65536)


def test_no_notification_while_pane_stays_at_shell_pgrp(monkeypatch, tmp_path):
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}  # shell's own pgid throughout
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)

    obs.on_pane_created(pane)  # records shell_pgid = 100 (from pgrp map)
    for _ in range(5):
        clock.advance(0.25)
        obs.on_tick({1: pane}, focused_id=2)

    os.close(w)
    assert os.read(r, 65536) == b""
    os.close(r)


def test_command_finishes_unfocused_above_threshold_notifies_once(monkeypatch):
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)
    obs.threshold = 2.0

    obs.on_pane_created(pane)  # shell_pgid = 100
    obs.on_tick({1: pane}, focused_id=2)

    pgrp[pane.master_fd] = 555  # a command takes the foreground
    obs.on_tick({1: pane}, focused_id=2)

    clock.advance(3.0)  # command runs for 3s -- above the 2.0s threshold
    pgrp[pane.master_fd] = 100  # command finished, shell has it back
    obs.on_tick({1: pane}, focused_id=2)
    # marker arrives essentially immediately
    obs.on_pane_output(1, MARKER_PREFIX + b"3\x07", is_focused=False)
    obs.on_tick({1: pane}, focused_id=2)  # should notice marker + notify

    os.close(w)
    out = os.read(r, 65536)
    os.close(r)
    assert out.count(b"[pane 1 finished") == 1
    assert b"exit 3" in out


def test_no_notification_when_pane_is_focused(monkeypatch):
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)
    obs.threshold = 2.0

    obs.on_pane_created(pane)
    obs.on_tick({1: pane}, focused_id=1)
    pgrp[pane.master_fd] = 555
    obs.on_tick({1: pane}, focused_id=1)
    clock.advance(3.0)
    pgrp[pane.master_fd] = 100
    obs.on_tick({1: pane}, focused_id=1)  # pane 1 is the focused pane

    os.close(w)
    assert os.read(r, 65536) == b""
    os.close(r)


def test_fast_command_below_threshold_does_not_notify(monkeypatch):
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)
    obs.threshold = 2.0

    obs.on_pane_created(pane)
    obs.on_tick({1: pane}, focused_id=2)
    pgrp[pane.master_fd] = 555
    obs.on_tick({1: pane}, focused_id=2)
    clock.advance(0.1)  # well under threshold, e.g. `ls`
    pgrp[pane.master_fd] = 100
    obs.on_tick({1: pane}, focused_id=2)
    clock.advance(1.0)  # let the grace window lapse
    obs.on_tick({1: pane}, focused_id=2)

    os.close(w)
    assert os.read(r, 65536) == b""
    os.close(r)


def test_marker_stripped_from_displayed_output():
    obs = CompletionObserver(notify_fd=os.pipe()[1], desktop_notify=False)
    pane = FakePane(1)
    obs._states[1] = notifier_mod._PaneState(shell_pgid=100)

    visible = obs.on_pane_output(1, b"hello" + MARKER_PREFIX + b"0\x07" + b"world", is_focused=True)
    assert visible == b"helloworld"


def test_marker_split_across_two_chunks_still_detected_and_hidden():
    obs = CompletionObserver(notify_fd=os.pipe()[1], desktop_notify=False)
    pane = FakePane(1)
    state = notifier_mod._PaneState(shell_pgid=100)
    obs._states[1] = state

    part1 = b"before" + MARKER_PREFIX[:4]
    part2 = MARKER_PREFIX[4:] + b"7\x07" + b"after"

    visible1 = obs.on_pane_output(1, part1, is_focused=True)
    assert visible1 == b"before"  # partial marker prefix withheld

    visible2 = obs.on_pane_output(1, part2, is_focused=True)
    assert visible2 == b"after"
    assert state.pending_exit == 7


def test_shell_exit_while_busy_notifies_with_waitpid_exit_status(monkeypatch):
    # Simulates `sleep 5; exit 3`: the pane's shell itself terminates
    # while a foreground command was running -- no PROMPT_COMMAND marker
    # will ever arrive for this, so on_pane_closed must notify using the
    # exit status Part 1 already captured via waitpid.
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)
    obs.threshold = 2.0

    obs.on_pane_created(pane)
    obs.on_tick({1: pane}, focused_id=2)
    pgrp[pane.master_fd] = 555  # `sleep 5` takes the foreground
    obs.on_tick({1: pane}, focused_id=2)
    clock.advance(5.0)
    # shell process terminates directly (no marker ever comes)
    obs.on_pane_closed(1, exit_status=3, is_focused=False)

    os.close(w)
    out = os.read(r, 65536)
    os.close(r)
    assert out.count(b"[pane 1 finished") == 1
    assert b"exit 3" in out


def test_shell_exit_while_idle_does_not_notify(monkeypatch):
    # Someone just types `exit` at an idle prompt -- nothing long ran, so
    # no notification should fire even though the pane closed.
    clock = FakeClock()
    pane = FakePane(1)
    pgrp = {pane.master_fd: 100}
    r, w = os.pipe()
    obs = make_observer(monkeypatch, clock, pgrp, w)

    obs.on_pane_created(pane)
    obs.on_tick({1: pane}, focused_id=2)  # never went busy
    obs.on_pane_closed(1, exit_status=0, is_focused=False)

    os.close(w)
    assert os.read(r, 65536) == b""
    os.close(r)
