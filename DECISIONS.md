# DECISIONS.md

## Part 1 — the multiplexer

**Event loop.** A single-threaded `select()` loop over `[stdin] +
[each alive pane's master fd]`, 0.25s timeout, one process. `select`
over `poll`/`asyncio`: the fd set is tiny (a handful of PTYs), so
`select`'s O(n) scan costs nothing, and `asyncio` fights the model —
PTY reads are blocking `os.read`, and `pty.fork()`/raw ioctls don't sit
well under asyncio's process abstractions. One process, not
thread-per-pane: `select()` already tells us which fds are *actually*
ready, so a firehose in one pane never blocks draining another —
verified by running `yes | head -c 50000000` unfocused while confirming
sub-10ms keystroke response in the focused pane. The 0.25s timeout
(rather than blocking indefinitely) exists so `SIGWINCH` — which only
sets a flag inside the signal handler, real ioctl work happens in the
main loop, to avoid doing real work inside a signal handler — gets
noticed promptly even when otherwise idle. Bytes are forwarded raw,
one `os.read`/`os.write` chunk at a time, never buffered or
line-parsed, since full-screen programs need byte-exact, low-latency
I/O.

**PTY / session leadership.** `pty.fork()`, not hand-rolled
`fork+setsid+TIOCSCTTY` — the stdlib already does this correctly, and
reimplementing it only risks getting the ordering wrong for no benefit.
Verified: Ctrl-C in a pane interrupts only that pane's foreground job.

**Focus model.** One visible pane at a time, prefix-key switching —
simultaneous split rendering needs a full screen model/escape parser,
explicitly Part 3 scope. Accepted consequence: switching to a pane
running a backgrounded full-screen program shows only new output, not
its current screen — partially mitigated (not solved) by toggling the
pane's reported size on switch to trigger a SIGWINCH repaint in
programs that handle it. New panes take focus immediately (matches
tmux, avoids the first keystroke after creation going to the wrong
shell).

**Prefix key: Ctrl-B**, not Ctrl-A, because Ctrl-A is "start of line" in
emacs-mode readline (unconscious muscle memory for most shell users);
Ctrl-B is also tmux's own default.

**Extension point for Part 2.** `Multiplexer` takes an `observer`
(default: no-op), called on every pane-output chunk, focus change, pane
creation, and pane close, and allowed to transform displayed output.
This was designed in during Part 1 specifically so Part 2 would not
need to restructure the event loop — and it didn't.

**Cut for time:** configurable prefix key/shell path (hardcoded), a
persistent status bar (needs escape parsing), handling every signal
(only SIGTERM/SIGHUP restore the terminal; SIGKILL can't be caught by
any process — an OS guarantee, not a gap).

---

## Part 2 — completion notifications

**Definition of "a command finished":** the terminal's foreground
process group reverting from something-that-isn't-the-shell back to the
shell's own pgid — a real kernel fact, polled via `TIOCGPGRP` every
event-loop tick (piggybacking on the existing 0.25s timeout).
Considered and rejected: parsing prompts from output (breaks on
customized `PS1`), a shell alias wrapper (only catches commands typed
through it), pure output-quiescence timing (wrong for silent long
commands and idle REPLs). pgrp tracking is the one option that's a
kernel fact, not a guess, and it's shell-agnostic.

pgrp tells us *when*, not the exit code — we're not the parent of
foreground commands, so there's no code to `waitpid()` on directly. For
that, `pane.py` injects a small hook at shell startup: bash gets
`--rcfile <generated>` that sources the user's real `.bashrc` then
*prepends* a `PROMPT_COMMAND` entry emitting `\x1b]9278;<$?>\x07` (must
run before any of the user's own hooks, or they could clobber `$?`
first); zsh gets an equivalent via a temporary `ZDOTDIR`/`precmd_functions`.
9278 is an invented, unregistered id, stripped unconditionally from
every pane's stream before display, with a small per-pane buffer so a
marker split across two reads is still recognised (matching only our
own marker's prefix, so a real ANSI sequence split at a read boundary
is never mistakenly withheld).

**A second path was necessary.** `sleep 5; exit 3` typed at a prompt
ends the shell itself — `exit` never lets the shell print another
prompt, so `PROMPT_COMMAND` never runs and no marker is ever emitted.
`on_pane_closed` covers this using the exact status Part 1 already
gets via `waitpid()`, triggered only if the pane was "busy" (a
non-shell pgrp held focus) at the moment it closed. Two paths, not
redundant: one applies exactly when the other structurally cannot.

**Threshold: 2.0s** (constant, not measured) — comfortably clears
`sleep 5`-style waits while letting `ls`/`cd`/`echo` pass silently.
Never notifies the currently-focused pane, checked at the moment of
completion, not the moment the command started.

**Surfacing:** status line + bell on the real terminal (guaranteed,
no dependency), plus best-effort `notify-send`/`osascript` via
`Popen` without `wait()` so a missing binary can't stall the loop.

**Failure modes, honestly stated:**
- Backgrounded jobs (`cmd &`) never take the foreground pgrp — invisible
  to this mechanism entirely.
- A compound line (`a; b; c`) yields one notification per handoff, i.e.
  possibly several notifications for one Enter press — correct under
  the stated definition, but could surprise someone expecting one.
- A daemonizing process that hands focus back early is reported
  "finished" while real work continues invisibly — inherent to
  pgrp-based detection.
- Non-bash/zsh shells get correct timing (pgrp tracking doesn't care
  what's running) but `exit unknown` instead of a real status.
- If the marker never arrives (grace window expires), we still notify
  with unknown status rather than staying silent — a notification
  missing a code beats no notification.

**Two real bugs found in testing** (both caught only by real-PTY
integration tests, not the faked-pgrp unit tests — the clearest
argument for keeping both kinds):
1. `$SHELL` unset → fell back to `/bin/sh`, which is `dash` here, with
   no `PROMPT_COMMAND` equivalent — marker path silently never fired.
   Fixed: prefer `bash` on `$PATH` over a bare `/bin/sh` fallback.
2. A genuine race: querying `TIOCGPGRP` right after `pane.start()`
   returned raced the child's own `os.setsid()`, observed returning `0`
   — since no real pgrp is ever `0`, every pane looked permanently
   "busy" and nothing was ever detected. Fixed by not querying at all:
   `pty.fork()`'s child is guaranteed `pgid == pid` before it execs, so
   `pane.pid` is the correct value with no race window.

**Cut for time:** exit-status hooks for fish/dash/csh; coalescing
multiple simultaneous notifications from different panes; any richer
notion of "focused" than "was this the focused pane at the instant of
completion."
