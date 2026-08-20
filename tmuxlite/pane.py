"""A single pane: one PTY plus one shell process.

Design decision (see DECISIONS.md): we use pty.fork() rather than hand-
rolling os.fork() + os.setsid() + TIOCSCTTY ourselves. pty.fork() already
puts the child in its own session with the slave PTY as its controlling
terminal, which is exactly the "session leader" requirement called out in
the assignment brief -- reimplementing it by hand would just reproduce
stdlib code with more chances to get the setsid/TIOCSCTTY ordering wrong.
"""
import os
import pty
import signal

from . import terminal


class Pane:
    def __init__(self, pane_id, shell=None, rows=24, cols=80):
        self.pane_id = pane_id
        self.shell = shell or os.environ.get("SHELL", "/bin/sh")
        self.rows = rows
        self.cols = cols
        self.pid = None
        self.master_fd = None
        self.alive = False
        self.exit_status = None

    def start(self):
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: stdio is now the slave PTY, and pty.fork() has
            # already made us a session leader with it as our controlling
            # terminal. Just exec the shell.
            os.execvp(self.shell, [self.shell])
            os._exit(127)  # only reached if execvp itself fails
        self.pid = pid
        self.master_fd = master_fd
        self.alive = True
        terminal.set_winsize(self.master_fd, self.rows, self.cols)
        return self

    def fileno(self):
        return self.master_fd

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        if self.alive:
            terminal.set_winsize(self.master_fd, rows, cols)

    def read(self, nbytes=65536):
        """Read available pane output.

        Returns the bytes read, or b"" once the pane's shell has exited
        (at which point self.alive becomes False and self.exit_status is
        populated).
        """
        if not self.alive:
            return b""
        try:
            data = os.read(self.master_fd, nbytes)
        except OSError:
            data = b""
        if data == b"":
            self._mark_dead()
        return data

    def write(self, data):
        if not self.alive:
            return
        try:
            os.write(self.master_fd, data)
        except OSError:
            self._mark_dead()

    def _mark_dead(self):
        if not self.alive:
            return
        self.alive = False
        try:
            # Simplification (documented in DECISIONS.md): we block here
            # rather than using WNOHANG. By the time os.read() on the
            # master fd returns EOF, the slave side has already lost its
            # last open reference, which for a plain shell-as-session-
            # leader pane means the child is exiting or already exited --
            # so this wait is effectively instantaneous in practice.
            _, status = os.waitpid(self.pid, 0)
            self.exit_status = self._decode_status(status)
        except ChildProcessError:
            self.exit_status = None
        try:
            os.close(self.master_fd)
        except OSError:
            pass

    @staticmethod
    def _decode_status(status):
        if os.WIFEXITED(status):
            return os.WEXITSTATUS(status)
        if os.WIFSIGNALED(status):
            return -os.WTERMSIG(status)
        return None

    def close(self):
        """Ask the pane's shell to go away (used when killing a pane, or
        during shutdown to make sure no shells are left behind)."""
        if self.alive:
            try:
                os.kill(self.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass
        self._mark_dead()
