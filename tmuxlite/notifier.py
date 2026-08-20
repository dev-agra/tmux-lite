"""Part 2: completion detection and notification.

See DECISIONS.md for the full reasoning. Summary of the design:

- "A command finished" is defined as: the pty's foreground process group
  reverted from something-that-isn't-the-shell back to the shell's own
  pgid. This is a real kernel fact (tracked via TIOCGPGRP), not a guess
  about shell output. We poll it every event-loop tick, piggybacking on
  Part 1's existing 0.25s select() timeout rather than adding a second
  timer.

- The pgrp fact alone doesn't carry an exit code -- we're not the parent
  of the commands the shell runs, so we can't waitpid() on them. Pane.py
  optionally injects a small PROMPT_COMMAND/precmd hook into bash/zsh at
  startup that emits an invented, unregistered OSC-style marker carrying
  $?. This module scans every pane's raw output for that marker, strips
  it before it ever reaches the real screen, and uses it as the exit
  code for whichever completion is currently pending. If no marker shows
  up within a short grace window (unsupported shell, or the hook
  injection silently failed), we still notify, just with an unknown exit
  status rather than staying silent.
"""
import fcntl
import os
import re
import shutil
import struct
import subprocess
import sys
import termios
import time


MARKER_PREFIX = b"\x1b]9278;"
MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + rb"(\d+)\x07")
# 9278 is not a real registered OSC code -- it's invented specifically to
# be distinctive and vanishingly unlikely to collide with real program
# output, since we strip it unconditionally wherever it appears.

DEFAULT_THRESHOLD_SECONDS = 2.0
# Trivial/fast commands (ls, cd, echo, git status) finish in well under a
# second; anything a user would plausibly alt-tab away from -- a build, a
# test run, `sleep 5` -- comfortably clears 2s. This is a tunable
# constant, not derived from any measurement of real command durations,
# and is documented as such.

MARKER_GRACE_SECONDS = 0.5
# How long we wait for the exit-status marker to arrive after we detect
# the pgrp handoff back to the shell, before giving up and reporting the
# completion with an unknown exit status.


def _get_foreground_pgrp(fd):
    try:
        buf = fcntl.ioctl(fd, termios.TIOCGPGRP, struct.pack("i", 0))
    except OSError:
        return None
    return struct.unpack("i", buf)[0]


def _partial_marker_tail_len(buf):
    """Return the length of a trailing prefix of MARKER_PREFIX (+ digits,
    no terminator yet) at the end of buf, or 0 if there is none.

    This only ever recognises a prefix of *our own* marker syntax -- not
    generic escape sequences -- so a legitimate ANSI/CSI sequence that
    happens to be split across two read() calls is never mistakenly held
    back. Only bytes that could plausibly become one of our own markers
    get buffered for the next chunk.
    """
    max_len = len(MARKER_PREFIX) + 12  # prefix + a generous digit count
    n = min(len(buf), max_len)
    for start in range(len(buf) - n, len(buf)):
        tail = buf[start:]
        if MARKER_PREFIX.startswith(tail):
            return len(tail)
        if tail.startswith(MARKER_PREFIX):
            rest = tail[len(MARKER_PREFIX):]
            if rest == b"" or rest.isdigit():
                return len(tail)
    return 0


class _PaneState:
    def __init__(self, shell_pgid):
        self.shell_pgid = shell_pgid
        self.last_pgrp = shell_pgid
        self.busy = False
        self.busy_since = None
        self.awaiting_marker_since = None
        self.pending_exit = None
        self.buf = b""  # partial-marker reassembly, see _partial_marker_tail_len


class CompletionObserver:
    """Watches pane output and pgrp transitions; decides when to notify.

    Plugs into Multiplexer via the `observer=` constructor argument,
    replacing NullObserver. Nothing about the event loop changes -- this
    is purely an additional subscriber to events the loop already
    produces (on_pane_created/on_pane_output/on_tick/on_focus_change/
    on_pane_closed), plus an output transform (on_pane_output returns
    the bytes to actually display, with our marker stripped out).
    """

    def __init__(self, notify_fd=None, threshold=DEFAULT_THRESHOLD_SECONDS,
                 desktop_notify=True):
        self.notify_fd = notify_fd if notify_fd is not None else sys.stdout.fileno()
        self.threshold = threshold
        self._desktop_notify_bin = self._find_desktop_notifier() if desktop_notify else None
        self._states = {}  # pane_id -> _PaneState

    # ---- lifecycle hooks --------------------------------------------------

    def on_pane_created(self, pane):
        # pty.fork()'s child always ends up as its own session/process-
        # group leader (setsid() makes pgid == pid) before it execs the
        # shell, and a shell doesn't change its own pgid afterwards --
        # it only ever changes the *terminal's foreground* pgrp when it
        # hands control to a job. So pane.pid is the shell's resting
        # pgid, deterministically.
        #
        # We deliberately do NOT query TIOCGPGRP here to "confirm" this:
        # doing so immediately after pane.start() returns races the
        # child's own os.setsid() (which hasn't necessarily run yet at
        # this point in the parent), and was observed in testing to
        # return 0, which is never equal to any real pgrp -- silently
        # making every pane look permanently "busy" from tick one.
        self._states[pane.pane_id] = _PaneState(shell_pgid=pane.pid)

    def on_pane_closed(self, pane_id, exit_status, is_focused):
        """A pane's shell process itself terminated.

        This is a distinct completion path from on_tick's pgrp-revert
        detection below, and it matters: something like `sleep 5; exit 3`
        typed at a prompt doesn't hand control back to a running shell --
        `exit` ends the shell itself, so it never reaches the point of
        printing a new prompt, which means our marker hook (tied to
        PROMPT_COMMAND/precmd) never fires for it. But if the pane was
        "busy" (a non-shell process group held the foreground) right up
        until it closed, that's a real completion, and the exit status
        Part 1 already captures precisely via waitpid is exactly what we
        want -- no marker needed, because none was ever going to arrive.

        If the pane was *not* busy when it closed (e.g. someone just typed
        `exit` at an idle prompt with nothing preceding it), there's no
        long-running command to report, so we stay silent -- consistent
        with "trivial things don't produce notifications".
        """
        state = self._states.pop(pane_id, None)
        if state is None or not state.busy:
            return
        duration = time.monotonic() - state.busy_since
        self._maybe_notify(pane_id, exit_status, duration, is_focused=is_focused)

    def on_focus_change(self, old_pane_id, new_pane_id):
        pass  # nothing to do: on_tick already re-checks focus at
              # notification time, which is the definition of "currently
              # focused" we're using -- see DECISIONS.md

    # ---- output scanning: strip our marker, remember any exit code -------

    def on_pane_output(self, pane_id, data, is_focused):
        state = self._states.get(pane_id)
        if state is None:
            return data

        combined = state.buf + data
        for m in MARKER_RE.finditer(combined):
            state.pending_exit = int(m.group(1))
        cleaned = MARKER_RE.sub(b"", combined)

        tail_len = _partial_marker_tail_len(cleaned)
        if tail_len:
            state.buf = cleaned[-tail_len:]
            cleaned = cleaned[:-tail_len]
        else:
            state.buf = b""

        return cleaned

    # ---- periodic pgrp polling --------------------------------------------

    def on_tick(self, panes, focused_id):
        now = time.monotonic()
        for pane_id, pane in list(panes.items()):
            state = self._states.get(pane_id)
            if state is None or not pane.alive:
                continue

            pgrp = _get_foreground_pgrp(pane.master_fd)
            if pgrp is None or pgrp == state.last_pgrp:
                pass
            else:
                if pgrp != state.shell_pgid and not state.busy:
                    # Some other process group just took the foreground:
                    # a command started running.
                    state.busy = True
                    state.busy_since = now
                    state.pending_exit = None
                    state.awaiting_marker_since = None
                elif pgrp == state.shell_pgid and state.busy:
                    # Control just came back to the shell: the command
                    # finished. We may not have its exit-status marker
                    # yet -- give it a short grace window (below).
                    state.awaiting_marker_since = now
                state.last_pgrp = pgrp

            if state.busy and state.awaiting_marker_since is not None:
                have_marker = state.pending_exit is not None
                grace_expired = (now - state.awaiting_marker_since) > MARKER_GRACE_SECONDS
                if have_marker or grace_expired:
                    duration = now - state.busy_since
                    self._maybe_notify(
                        pane_id, state.pending_exit, duration,
                        is_focused=(pane_id == focused_id),
                    )
                    state.busy = False
                    state.busy_since = None
                    state.awaiting_marker_since = None
                    state.pending_exit = None

    # ---- decision + surfacing ----------------------------------------------

    def _maybe_notify(self, pane_id, exit_code, duration, is_focused):
        # Hard requirements from the brief, both checked right here:
        if is_focused:
            return
        if duration < self.threshold:
            return
        self._notify(pane_id, exit_code, duration)

    def _notify(self, pane_id, exit_code, duration):
        status_text = str(exit_code) if exit_code is not None else "unknown"
        msg = (
            f"\r\n\a[pane {pane_id} finished: exit {status_text}, "
            f"{duration:.1f}s]\r\n"
        ).encode()
        try:
            os.write(self.notify_fd, msg)
        except OSError:
            pass
        if self._desktop_notify_bin:
            self._desktop_notify_async(pane_id, status_text)

    def _desktop_notify_async(self, pane_id, status_text):
        # Best-effort and non-blocking: Popen without wait(), and any
        # failure to launch is swallowed. This is a bonus channel, not
        # the primary one, so it must never be able to stall the event
        # loop or crash the program.
        try:
            if self._desktop_notify_bin == "notify-send":
                subprocess.Popen(
                    ["notify-send", f"tmux-lite: pane {pane_id} finished",
                     f"exit status {status_text}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            elif self._desktop_notify_bin == "osascript":
                script = (
                    f'display notification "exit status {status_text}" '
                    f'with title "tmux-lite: pane {pane_id} finished"'
                )
                subprocess.Popen(
                    ["osascript", "-e", script],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
        except OSError:
            pass

    @staticmethod
    def _find_desktop_notifier():
        if shutil.which("notify-send"):
            return "notify-send"
        if shutil.which("osascript"):
            return "osascript"
        return None
