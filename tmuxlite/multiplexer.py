"""Pane collection, focus management, prefix-key commands, and the event loop.

This module is the extension point for Part 2. Every time the event loop
reads bytes from a pane's PTY, and every time focus changes or a pane
closes, it calls a method on `self.observer`. Part 1 ships `NullObserver`,
which does nothing. Part 2 will supply an observer that watches pgrp
changes / timing on `on_pane_output` and calls back into a notifier when
it decides a command has finished in an unfocused pane. Nothing about the
event loop's structure should need to change when that lands -- it's
purely an additional subscriber to events the loop already produces.
"""
import os
import select
import sys

from . import terminal
from .pane import Pane


PREFIX_KEY = b"\x02"  # Ctrl-B.
# Chosen over the tmux default (Ctrl-B is tmux's default too, so this is
# unsurprising) and over Ctrl-A because Ctrl-A collides with "move to
# start of line" in emacs-mode readline/shell bindings, which almost
# everyone relies on unconsciously. Documented here per the assignment's
# request to justify the choice.


class NullObserver:
    """Default no-op observer. Part 2 will replace/subclass this."""

    def on_pane_created(self, pane):
        pass

    def on_pane_output(self, pane_id, data, is_focused):
        return data  # identity: Part 1 doesn't need to transform output

    def on_focus_change(self, old_pane_id, new_pane_id):
        pass

    def on_pane_closed(self, pane_id, exit_status, is_focused):
        pass

    def on_tick(self, panes, focused_id):
        pass


class Multiplexer:
    def __init__(self, shell=None, observer=None, status_out=None):
        self.shell = shell
        self.panes = {}
        self.focused_id = None
        self._next_id = 1
        self.observer = observer or NullObserver()
        self._await_command = False
        self._resize_pending = False
        # status_out lets tests capture status-line writes instead of
        # hitting the real stdout fd.
        self._status_out = status_out if status_out is not None else sys.stdout.fileno()

    # ---- pane lifecycle --------------------------------------------------

    def create_pane(self):
        rows, cols = terminal.get_winsize(sys.stdin.fileno())
        pane = Pane(self._next_id, shell=self.shell, rows=rows, cols=cols)
        pane.start()
        self.panes[pane.pane_id] = pane
        self._next_id += 1
        self.observer.on_pane_created(pane)
        # New panes take focus immediately (matches tmux's behaviour and
        # is what a user pressing "create pane" expects to happen).
        old_id = self.focused_id
        self.focused_id = pane.pane_id
        self.observer.on_focus_change(old_id, pane.pane_id)
        return pane

    def focused_pane(self):
        return self.panes.get(self.focused_id)

    def switch(self, direction=1):
        """Move focus to the next (direction=1) or previous (-1) pane,
        cycling by pane id. A no-op with 0 or 1 panes."""
        ids = sorted(self.panes.keys())
        if not ids:
            return
        if self.focused_id not in ids:
            new_id = ids[0]
        else:
            idx = ids.index(self.focused_id)
            new_id = ids[(idx + direction) % len(ids)]
        old_id = self.focused_id
        self.focused_id = new_id
        self.observer.on_focus_change(old_id, new_id)
        if new_id != old_id:
            self._kick_redraw(self.panes[new_id])
        self._write_status()

    def kill_focused(self):
        pane = self.focused_pane()
        if pane:
            pane.close()
            self._reap(pane)

    def _reap(self, pane):
        # Captured before popping: whether this pane was the focused one
        # at the moment it closed is what "currently focused" means for
        # the purposes of suppressing a notification (see DECISIONS.md).
        is_focused = pane.pane_id == self.focused_id
        self.panes.pop(pane.pane_id, None)
        self.observer.on_pane_closed(pane.pane_id, pane.exit_status, is_focused)
        if self.focused_id == pane.pane_id:
            self.focused_id = next(iter(self.panes), None)
            self._write_status()

    def _kick_redraw(self, pane):
        """Nudge full-screen apps to repaint after switching to them.

        We don't maintain a screen model (that's explicitly out of scope
        for Part 1 -- see README), so switching to a pane that's been
        running vim/htop in the background only shows *new* output, not
        its current screen state. As a partial mitigation, briefly
        toggling the pane's reported size delivers SIGWINCH to it, which
        prompts most curses-based programs to repaint. This is a
        heuristic, not a guarantee: programs that don't handle SIGWINCH
        won't repaint, and this is called out as a known limitation.
        """
        rows, cols = pane.rows, pane.cols
        if cols > 1 and pane.alive:
            pane.resize(rows, cols - 1)
            pane.resize(rows, cols)

    def _write_status(self):
        pane = self.focused_pane()
        label = f"\r\n[pane {pane.pane_id if pane else '-'}]\r\n".encode()
        os.write(self._status_out, label)

    # ---- input handling ---------------------------------------------------

    def handle_stdin_bytes(self, data):
        """Run raw stdin bytes through the prefix-key state machine.

        Split out from run() so it can be unit-tested without a real
        terminal or PTY (see tests/test_multiplexer.py).
        """
        for i in range(len(data)):
            byte = data[i:i + 1]
            if self._await_command:
                self._await_command = False
                self._run_command(byte)
            elif byte == PREFIX_KEY:
                self._await_command = True
            else:
                pane = self.focused_pane()
                if pane:
                    pane.write(byte)

    def _run_command(self, byte):
        if byte == PREFIX_KEY:
            # Prefix key pressed twice: send one literal prefix byte
            # through to the shell. This is the "way for the user to send
            # a literal prefix key through" the brief asks for.
            pane = self.focused_pane()
            if pane:
                pane.write(PREFIX_KEY)
        elif byte == b"c":
            self.create_pane()
            self._write_status()
        elif byte == b"n":
            self.switch(1)
        elif byte == b"p":
            self.switch(-1)
        elif byte == b"x":
            self.kill_focused()
        else:
            # Unrecognised command key: bell, drop back to normal mode.
            # We deliberately don't send it to the shell -- the user just
            # pressed prefix + a key expecting multiplexer behaviour, so
            # silently forwarding it as a literal keystroke would be more
            # surprising than a bell.
            os.write(self._status_out, b"\a")

    # ---- resize -------------------------------------------------------------

    def handle_resize(self):
        rows, cols = terminal.get_winsize(sys.stdin.fileno())
        pane = self.focused_pane()
        if pane:
            pane.resize(rows, cols)

    def request_resize(self):
        """Called from the SIGWINCH handler. Just sets a flag -- see
        DECISIONS.md for why the actual resize is deferred to the main
        loop rather than done inside the signal handler."""
        self._resize_pending = True

    # ---- main loop ----------------------------------------------------------

    def run(self):
        stdin_fd = sys.stdin.fileno()
        stdout_fd = sys.stdout.fileno()
        self.create_pane()
        self._write_status()

        while self.panes:
            if self._resize_pending:
                self._resize_pending = False
                self.handle_resize()

            watch = [stdin_fd] + [p.master_fd for p in self.panes.values() if p.alive]
            try:
                # A finite timeout (rather than blocking indefinitely)
                # lets us notice self._resize_pending even on platforms/
                # timings where select() doesn't get interrupted between
                # SIGWINCH delivery and our next check.
                ready, _, _ = select.select(watch, [], [], 0.25)
            except InterruptedError:
                continue

            if stdin_fd in ready:
                data = os.read(stdin_fd, 4096)
                if data:
                    self.handle_stdin_bytes(data)

            for pane in list(self.panes.values()):
                if pane.master_fd in ready:
                    data = pane.read()
                    if not pane.alive:
                        self._reap(pane)
                        continue
                    is_focused = pane.pane_id == self.focused_id
                    # on_pane_output may strip an invisible marker (Part 2)
                    # before the bytes ever reach the real screen.
                    data = self.observer.on_pane_output(pane.pane_id, data, is_focused)
                    if is_focused and data:
                        os.write(stdout_fd, data)

            # Runs every tick regardless of I/O (piggybacking on the
            # select() timeout above) so pgrp-transition polling for Part 2
            # doesn't need a second timer -- see DECISIONS.md.
            self.observer.on_tick(self.panes, self.focused_id)

    def shutdown(self):
        """Best-effort cleanup: make sure no pane shells are left running
        if we're exiting for a reason other than 'all panes closed
        themselves' (e.g. an exception escaped run())."""
        for pane in list(self.panes.values()):
            pane.close()
        self.panes.clear()
