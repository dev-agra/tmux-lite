# DECISIONS.md — Part 1

Scope note: this document covers Part 1 only (the multiplexer). Part 2's
decisions (completion detection, its failure modes, the definition of
"a command finished") are not here because Part 2 isn't built yet — see
README.md "What's next." I'm covering here what the brief asks for in
scope of what's actually implemented: event loop structure, and what was
knowingly cut.

## Event loop structure, and why

A single-threaded `select()` loop over `[stdin_fd] + [pane.master_fd for
each alive pane]`, with a 0.25s timeout, running in the main process.

**Why `select()` over `poll()` or `asyncio`:**
- The fd set here is tiny (stdin + a handful of pane PTYs). `select`'s
  O(n) scan is irrelevant at this scale, and it's the simplest, most
  portable option across Linux/macOS (the two platforms actually
  targeted; `poll` is fine on both but buys nothing here).
- `asyncio` fights the model: PTY master fds are readable via blocking
  `os.read`, and forking children mid-event-loop is awkward with
  asyncio's process/subprocess abstractions layered on top of what we
  need to do manually anyway (`pty.fork`, raw ioctls). A plain loop is
  more legible for a program this size.

**Why a single process, not one thread/process per pane:**
- The "unfocused pane must not stall the focused pane" requirement is
  satisfied by `select()` telling us which fds are *actually* ready —
  we never block on a specific pane's fd, so a firehose in pane 2 doesn't
  hold up draining pane 1. Threads-per-pane would add lock/queue
  complexity to solve a problem `select` already solves for free at this
  scale. Verified empirically: a background `yes | head -c 50000000`
  in an unfocused pane doesn't measurably delay a keystroke round-trip
  in the focused one (sub-10ms).

**Why a 0.25s timeout instead of blocking indefinitely:**
- `SIGWINCH` is handled by a Python signal handler that just sets a flag
  (`Multiplexer.request_resize()`); the actual resize (an ioctl call) is
  done from the main loop, not inside the signal handler. This is
  deliberate: doing real work inside a signal handler while we might be
  in the middle of a `select()` call risks reentrancy issues, and
  Python's own signal-handling model (PEP 475) already retries
  interrupted syscalls transparently, so a raw `except InterruptedError`
  around `select()` isn't reliably reached. The timeout is what
  guarantees we notice the flag promptly instead of only on the next
  fd-readiness event, which might be far in the future if the terminal
  is otherwise idle.
- The cost is a wakeup every 0.25s even when idle — negligible CPU for a
  program with a handful of fds, and small enough that a resize feels
  effectively instant to a human.

**Why bytes are forwarded raw, unbuffered, one `os.read()`/`os.write()`
chunk at a time, with no line buffering or coalescing:**
- Anything else would filter/delay what the pane produces, breaking
  full-screen programs that expect byte-exact, low-latency I/O (cursor
  positioning, partial redraws). We just move whatever chunk the kernel
  handed us.

## PTY / session leadership: `pty.fork()` vs manual `fork+setsid+ioctl`

Used `pty.fork()` rather than hand-rolling `os.fork()` +
`os.setsid()` + `TIOCSCTTY` + dup2'ing the slave onto 0/1/2. `pty.fork()`
already does exactly this in the stdlib, correctly ordered. The brief's
hint about job control requiring session-leader + controlling-terminal
setup is real, but reimplementing already-correct stdlib code by hand
would only add a chance to get the ordering wrong (e.g. setsid must
happen before TIOCSCTTY, before the terminal has a controlling process)
for no benefit. Verified job control works via manual testing: Ctrl-C
inside a pane interrupts the foreground command in that pane, not the
whole multiplexer.

## Focus model: one visible pane, prefix-key switching

Chose the fully-acceptable-per-brief option of one pane visible at a
time, rather than attempting simultaneous split rendering. Split
rendering needs a terminal-escape-sequence parser and a screen model to
know what a background pane's current display *is*, in order to render
it into a sub-region of the real screen — that's Part 3-level scope
(explicitly called out as bonus, requiring "escape sequence parsing,
screen model, alternate screen buffer"). Attempting it inside Part 1's
time budget would risk a half-working version of both instead of a
solid version of one.

**Consequence knowingly accepted:** switching to a backgrounded
full-screen program shows only new output, not its current screen state,
until it next redraws. Mitigated cheaply (not solved) by toggling the
pane's reported size on focus switch to trigger a SIGWINCH-driven
repaint in programs that handle it. Documented in README as a known
limitation rather than presented as solved.

**New panes take focus immediately.** Matches tmux's own behaviour and
what a user pressing "create pane" expects — if it didn't, the first
keystroke after creating a pane would go to the wrong shell, which is a
worse surprise than "the marker says pane 2 now."

## Prefix key choice: Ctrl-B, not Ctrl-A

Ctrl-A is bound to "move to start of line" in emacs-mode readline (the
default in bash/zsh), which is used unconsciously by most people who
touch a shell. Rebinding it would mean every "go to start of line" during
normal shell use inside a pane needs the double-prefix escape, which is a
worse everyday cost than picking a different key. Ctrl-B is also tmux's
own default, so it should feel unsurprising to anyone who's used tmux.

## Extension point for Part 2

`Multiplexer` takes an `observer` (default: `NullObserver`, which does
nothing) and calls it on three events: `on_pane_output(pane_id, data,
is_focused)` on every chunk of output read from a pane, `on_focus_change`
on every switch, and `on_pane_closed` on every pane exit. Part 2 is
intended to be a new observer implementation plugged in here, not a
restructuring of the event loop — the loop already produces every event
Part 2 needs (in particular, `is_focused` is already known at the point
we'd want to suppress a notification for the focused pane).

This was a deliberate design choice made *now*, while building Part 1,
specifically because the assignment says the multiplexer is "the
substrate" for Part 2 — so Part 1's structure was chosen to not need
rework once Part 2 lands.

## What was knowingly cut from Part 1 for time

- **Configurable prefix key / shell path.** Hardcoded (`\x02`,
  `$SHELL`/`/bin/sh`). Would be a CLI-flag or config-file addition; not
  attempted since it's not part of what's being assessed.
- **A real status bar.** Currently a printed `[pane N]` line on focus
  change, which scrolls into history like any other output rather than
  staying pinned. A pinned bar needs escape-sequence handling, which is
  Part 3 scope.
- **Handling every possible fatal signal for restoration.** Only
  SIGTERM/SIGHUP are caught. SIGKILL can't be caught by any process
  (an OS guarantee, not a gap we could close), and other signals
  (SIGQUIT, SIGABRT from an actual bug) were judged lower priority than
  covering the two most realistic "something outside politely asked us
  to stop" cases within the assignment's suggested time budget.
