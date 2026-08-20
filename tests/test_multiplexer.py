"""Unit tests for the parts of the multiplexer that don't require a real
PTY or a real terminal.

Testability note (see README): most of Part 1 is inherently hard to unit
test because its correctness lives in kernel/tty behaviour (raw mode,
TIOCSWINSZ, session leadership, SIGWINCH delivery) that only manifests
against a real terminal. Rather than fake all of that unconvincingly, we
factor the genuinely pure logic -- the prefix-key state machine and pane
focus/id cycling -- out into methods that take plain bytes/ids and don't
touch a real fd, and test those directly here. The PTY-touching behaviour
(vim reflow, stty size, terminal restoration) is exercised by the manual
checklist in README.md instead, which is the more honest tool for it.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tmuxlite.multiplexer import Multiplexer, PREFIX_KEY


class FakePane:
    """Stands in for tmuxlite.pane.Pane in tests: no real fork/PTY."""

    def __init__(self, pane_id):
        self.pane_id = pane_id
        self.master_fd = -pane_id  # unique fake fd, never selected on in tests
        self.alive = True
        self.rows, self.cols = 24, 80
        self.written = b""
        self.exit_status = None

    def write(self, data):
        self.written += data

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols

    def close(self):
        self.alive = False


def make_mux_with_fake_panes(n):
    mux = Multiplexer(status_out=os.pipe()[1])  # write end we never read; discard
    for i in range(1, n + 1):
        pane = FakePane(i)
        mux.panes[i] = pane
    mux.focused_id = 1 if n else None
    return mux


def test_normal_bytes_go_to_focused_pane():
    mux = make_mux_with_fake_panes(1)
    mux.handle_stdin_bytes(b"ls\r")
    assert mux.panes[1].written == b"ls\r"


def test_prefix_then_unknown_key_does_not_reach_shell():
    mux = make_mux_with_fake_panes(1)
    mux.handle_stdin_bytes(PREFIX_KEY + b"z")
    assert mux.panes[1].written == b""


def test_double_prefix_sends_one_literal_prefix_byte():
    mux = make_mux_with_fake_panes(1)
    mux.handle_stdin_bytes(PREFIX_KEY + PREFIX_KEY)
    assert mux.panes[1].written == PREFIX_KEY


def test_prefix_n_switches_focus_forward_and_cycles():
    mux = make_mux_with_fake_panes(3)
    assert mux.focused_id == 1
    mux.handle_stdin_bytes(PREFIX_KEY + b"n")
    assert mux.focused_id == 2
    mux.handle_stdin_bytes(PREFIX_KEY + b"n")
    assert mux.focused_id == 3
    mux.handle_stdin_bytes(PREFIX_KEY + b"n")
    assert mux.focused_id == 1  # wraps back around


def test_prefix_p_switches_focus_backward():
    mux = make_mux_with_fake_panes(3)
    mux.handle_stdin_bytes(PREFIX_KEY + b"p")
    assert mux.focused_id == 3  # wraps the other way


def test_prefix_x_kills_focused_pane_and_moves_focus():
    mux = make_mux_with_fake_panes(2)
    mux.handle_stdin_bytes(PREFIX_KEY + b"x")
    assert 1 not in mux.panes
    assert mux.focused_id == 2
    assert mux.panes[2].alive


def test_killing_last_pane_leaves_no_focus():
    mux = make_mux_with_fake_panes(1)
    mux.handle_stdin_bytes(PREFIX_KEY + b"x")
    assert mux.panes == {}
    assert mux.focused_id is None


def test_bytes_split_across_two_reads_still_parse_correctly():
    # Guards against a state-machine bug where prefix detection only works
    # within a single read() call instead of persisting across calls.
    mux = make_mux_with_fake_panes(2)
    mux.handle_stdin_bytes(PREFIX_KEY)
    mux.handle_stdin_bytes(b"n")
    assert mux.focused_id == 2


def test_decode_status_exit_code():
    from tmuxlite.pane import Pane
    import os as _os

    pid = _os.fork()
    if pid == 0:
        _os._exit(3)
    _, status = _os.waitpid(pid, 0)
    assert Pane._decode_status(status) == 3


def test_decode_status_signal():
    from tmuxlite.pane import Pane
    import os as _os
    import signal as _signal
    import time

    pid = _os.fork()
    if pid == 0:
        _os.kill(_os.getpid(), _signal.SIGKILL)
        _os._exit(0)  # unreachable
    _, status = _os.waitpid(pid, 0)
    assert Pane._decode_status(status) == -_signal.SIGKILL

# Note: full end-to-end PTY behaviour (vim reflow, `stty size` inside a
# pane, terminal restoration on clean/unclean exit, unfocused-pane output
# not stalling the focused pane) is *not* unit tested here. That behaviour
# lives in kernel tty semantics that only manifest against a real
# terminal, and faking it convincingly would mostly test the fake, not
# our code. It's exercised instead via the manual checklist in README.md,
# which is the more honest tool for it.
