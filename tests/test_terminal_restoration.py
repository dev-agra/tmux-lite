"""Integration tests for the highest-stakes requirement in the brief:
terminal restoration on exit, including unclean exit.

Unlike test_multiplexer.py, this test deliberately does *not* mock
anything. It opens a real pty pair, execs the actual `tmuxlite.main`
attached to the slave as its controlling terminal (mirroring exactly how
a real shell would launch us), and inspects the slave's termios state
before and after -- from the parent's own fd on the same device -- to
confirm raw mode was actually entered and, crucially, actually undone.

This is the one place we lean on a real PTY in the test suite rather than
a fake, specifically because "did termios get restored" is not something
a fake can answer honestly.
"""
import os
import pty
import signal
import sys
import termios
import time

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _spawn_attached_to_new_pty():
    """Open a fresh pty pair and fork tmuxlite.main onto the slave as its
    controlling terminal, the same way a real shell would launch us.
    Returns (child_pid, master_fd, slave_fd, original_attrs).
    """
    master_fd, slave_fd = pty.openpty()
    original_attrs = termios.tcgetattr(slave_fd)

    pid = os.fork()
    if pid == 0:
        os.close(master_fd)
        os.setsid()
        # Make the slave our controlling terminal.
        import fcntl
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)
        os.chdir(REPO_ROOT)
        os.execvp(sys.executable, [sys.executable, "-m", "tmuxlite.main"])
        os._exit(1)

    return pid, master_fd, slave_fd, original_attrs


def _echo_and_icanon(attrs):
    lflag = attrs[3]
    return bool(lflag & termios.ECHO), bool(lflag & termios.ICANON)


def test_raw_mode_entered_then_restored_on_clean_exit():
    pid, master_fd, slave_fd, original = _spawn_attached_to_new_pty()
    try:
        time.sleep(0.4)  # let it enter raw mode
        mid_attrs = termios.tcgetattr(slave_fd)
        assert _echo_and_icanon(mid_attrs) == (False, False), (
            "expected ECHO and ICANON off while running (raw mode)"
        )

        # Ask the single pane's shell to exit -> zero panes -> clean exit.
        os.write(master_fd, b"exit\n")
        deadline = time.time() + 3
        while time.time() < deadline:
            done_pid, _ = os.waitpid(pid, os.WNOHANG)
            if done_pid == pid:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("process did not exit in time")

        final_attrs = termios.tcgetattr(slave_fd)
        assert _echo_and_icanon(final_attrs) == _echo_and_icanon(original), (
            "terminal was not restored to its original ECHO/ICANON state "
            "after clean exit"
        )
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_raw_mode_restored_even_when_killed_with_sigterm():
    pid, master_fd, slave_fd, original = _spawn_attached_to_new_pty()
    try:
        time.sleep(0.4)
        mid_attrs = termios.tcgetattr(slave_fd)
        assert _echo_and_icanon(mid_attrs) == (False, False)

        # Simulate an unclean kill, not a clean shell exit.
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 3
        while time.time() < deadline:
            done_pid, _ = os.waitpid(pid, os.WNOHANG)
            if done_pid == pid:
                break
            time.sleep(0.05)
        else:
            raise AssertionError("process did not exit after SIGTERM")

        final_attrs = termios.tcgetattr(slave_fd)
        assert _echo_and_icanon(final_attrs) == _echo_and_icanon(original), (
            "terminal was not restored after being killed with SIGTERM"
        )
    finally:
        os.close(master_fd)
        os.close(slave_fd)
