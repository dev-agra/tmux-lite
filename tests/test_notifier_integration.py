"""End-to-end integration tests for Part 2, driven against the real
`tmuxlite.main` entry point through a real pty, with real bash panes.

These exist because the unit tests in test_notifier.py fake out pgrp
polling and the clock -- which correctly proves the decision logic, but
would not have caught the actual bug found during manual testing: a race
where on_pane_created queried TIOCGPGRP before the freshly-forked child
had called os.setsid(), silently returning 0 and marking every pane
permanently "busy" from tick one. Only a real fork + real timing surfaces
that class of bug, so it's worth the slower, real-PTY version of these
checks living here permanently rather than only having been run by hand.
"""
import os
import pty
import select
import sys
import time

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _spawn_mux():
    pid, master_fd = pty.fork()
    if pid == 0:
        os.chdir(REPO_ROOT)
        os.execvp(sys.executable, [sys.executable, "-m", "tmuxlite.main"])
        os._exit(1)
    return pid, master_fd


def _send(master_fd, data, delay=0.2):
    time.sleep(delay)
    os.write(master_fd, data)


def _drain(master_fd, timeout=0.5):
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master_fd], [], [], 0.1)
        if master_fd in r:
            try:
                out += os.read(master_fd, 65536)
            except OSError:
                break
    return out


def _wait_for(master_fd, marker, timeout=8.0):
    out = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master_fd], [], [], 0.1)
        if master_fd in r:
            try:
                out += os.read(master_fd, 65536)
            except OSError:
                break
        if marker in out:
            return out, True
    return out, False


def _kill(pid, master_fd):
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def test_shell_exit_mid_command_notifies_once_with_correct_status():
    """`sleep 5; exit 3` in an unfocused pane: the literal graded
    checklist scenario. The `exit 3` ends the pane's shell itself, so
    this exercises the on_pane_closed path, not the marker path."""
    pid, master_fd = _spawn_mux()
    try:
        time.sleep(0.5)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02c")
        _drain(master_fd, 0.5)
        _send(master_fd, b"sleep 5; exit 3\n", 0.2)
        _send(master_fd, b"\x02p", 0.3)  # focus back to pane1
        out, found = _wait_for(master_fd, b"[pane 2 finished", timeout=8.0)
        assert found, out
        assert out.count(b"[pane 2 finished") == 1
        assert b"exit 3" in out
    finally:
        _kill(pid, master_fd)


def test_failing_command_notifies_with_nonzero_status_shell_survives():
    """A failing command where the shell keeps running afterwards:
    exercises the marker path (PROMPT_COMMAND), not pane closure."""
    pid, master_fd = _spawn_mux()
    try:
        time.sleep(0.5)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02c")
        _drain(master_fd, 0.5)
        _send(master_fd, b"(sleep 3; false)\n", 0.2)
        _send(master_fd, b"\x02p", 0.3)
        out, found = _wait_for(master_fd, b"[pane 2 finished", timeout=8.0)
        assert found, out
        assert out.count(b"[pane 2 finished") == 1
        assert b"exit 1" in out

        # confirm the shell is genuinely still alive (marker path, not a
        # pane closure) by running something in it afterwards
        _send(master_fd, b"\x02n", 0.3)
        _send(master_fd, b"echo STILL_ALIVE\n", 0.2)
        out2 = _drain(master_fd, 0.5)
        assert b"STILL_ALIVE" in out2
    finally:
        _kill(pid, master_fd)


def test_no_spurious_notification_while_sitting_in_long_foreground_process():
    """`cat` blocking on stdin stands in for an interactive full-screen
    program (vim/less/htop): foreground pgrp never reverts while you sit
    inside it, so no notification should fire during that time."""
    pid, master_fd = _spawn_mux()
    try:
        time.sleep(0.5)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02c")
        _drain(master_fd, 0.5)
        _send(master_fd, b"cat\n", 0.2)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02p", 0.3)  # away from pane2 while cat sits there
        out = _drain(master_fd, 6.0)
        assert b"[pane 2 finished" not in out
    finally:
        _kill(pid, master_fd)


def test_fast_command_does_not_notify():
    pid, master_fd = _spawn_mux()
    try:
        time.sleep(0.5)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02c")
        _drain(master_fd, 0.5)
        _send(master_fd, b"ls\n", 0.2)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02p", 0.3)
        out = _drain(master_fd, 4.0)
        assert b"[pane 2 finished" not in out
    finally:
        _kill(pid, master_fd)


def test_no_notification_for_currently_focused_pane():
    pid, master_fd = _spawn_mux()
    try:
        time.sleep(0.5)
        _drain(master_fd, 0.3)
        _send(master_fd, b"\x02c")  # pane2 created and focused
        _drain(master_fd, 0.5)
        _send(master_fd, b"sleep 3\n", 0.2)
        out = _drain(master_fd, 4.5)  # stay focused on pane2 throughout
        assert b"[pane 2 finished" not in out
    finally:
        _kill(pid, master_fd)
