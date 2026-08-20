# tmux-lite (Part 1)

A simplified terminal multiplexer. Python 3.11+, standard library only.

This submission covers **Part 1** (the multiplexer substrate) only. Part 2
(completion notifications) is not implemented yet — see "What's next"
below. Everything here is built so Part 2 can be added without changing
this code's structure (see `Multiplexer.observer` hooks).

## Running it

```
python3 -m tmuxlite.main
```

Run it from a real terminal (not redirected/piped). It will start your
`$SHELL` (falls back to `/bin/sh`) in pane 1.

## Prefix key and commands

Prefix key: **Ctrl-B** (`\x02`), chosen over Ctrl-A because Ctrl-A is
"move to start of line" in emacs-mode readline, which is muscle memory for
most shell users. tmux's own default is also Ctrl-B, so this should feel
familiar.

After pressing the prefix, the next key is a command:

| Key | Action |
|---|---|
| `c` | create a new pane (and focus it) |
| `n` | focus the next pane (wraps around) |
| `p` | focus the previous pane (wraps around) |
| `x` | kill the focused pane's shell |
| Ctrl-B (again) | send one literal `\x02` byte through to the shell |
| anything else | unrecognised — terminal bell, no-op |

A `[pane N]` marker is printed whenever focus changes, so you know where
you are. This is a plain printed line, not a persistent status bar — see
limitations below.

The program exits automatically once the last pane's shell exits.

## What works

- Single pane running a real shell, full interactive behaviour (tested
  manually with `vim`, `less`, `htop`, shell job control).
- `stty size` inside a pane reports that pane's dimensions, not the
  outer terminal's.
- Resizing the outer terminal reflows the *focused* pane's full-screen
  programs correctly (verified with `vim` and by checking `stty size`
  before/after a resize).
- Two or more independent panes: one shell exiting or misbehaving does
  not affect another. Verified by running a heavy, sustained firehose
  (`yes | head -c 50000000 > /dev/null`) in an unfocused pane and
  confirming the focused pane still responds to a keystroke in <10ms
  (see `tests/`, described below, and manual testing during
  development).
- Terminal restoration on clean exit **and** on being killed (SIGTERM).
  This is verified by an actual integration test
  (`tests/test_terminal_restoration.py`) that inspects the real PTY's
  termios flags before/during/after, not just by inspection of the code.
- Pane cleanup: a pane whose shell exits is removed from the pane list;
  the program exits once none remain.

## What does not work / known limitations

- **No screen model.** We don't parse escape sequences or keep a virtual
  screen per pane (this is explicitly out of scope for Part 1 per the
  brief). Practical consequence: switching to a pane that's been running
  a full-screen program in the background shows only *new* output from
  that point forward, not its current screen contents. As a partial
  mitigation, switching focus briefly toggles the pane's reported
  terminal size, which delivers SIGWINCH and prompts most curses-based
  programs (vim, htop, less) to repaint — but this is a heuristic, not a
  guarantee, and programs that ignore SIGWINCH won't repaint until they
  next redraw on their own.
- **No split-screen.** Only one pane is visible at a time; switching is
  how you move between them. This is explicitly acceptable per the
  brief.
- **Status line is a printed line, not a persistent bar.** A real status
  bar that survives full-screen redraws would require the same escape-
  sequence parsing we're deliberately not doing in Part 1.
- **Only SIGTERM/SIGHUP are caught for terminal restoration**, not every
  signal that could kill us (e.g. SIGKILL cannot be caught by any
  process — that's a hard OS limitation, not a gap in our handling).
- No detach/reattach, no scrollback/copy mode, no resizable layout tree —
  all explicitly Part 3, not attempted here.

## Tests

`tests/test_multiplexer.py` — unit tests for the parts of the system that
are pure logic and don't need a real terminal or PTY: the prefix-key
state machine (normal bytes pass through, prefix+key dispatches commands,
double-prefix sends a literal byte, bytes split across multiple `read()`
calls still parse correctly), pane-id focus cycling (next/prev, wrapping,
what happens to focus when the focused pane is killed, when the last pane
is killed), and exit-status decoding for normal exits and signals. These
run against a `FakePane` double — no real fork/PTY involved — so they're
fast and deterministic.

`tests/test_terminal_restoration.py` — integration tests that do use a
real PTY, specifically because "was the terminal actually restored" is
not something a fake can answer honestly. They open a real pty pair,
exec the actual program attached to it as its controlling terminal (the
same way a real shell would), and inspect the slave's termios ECHO/ICANON
flags before, during, and after — for both a clean exit and a SIGTERM
kill.

**What's deliberately not unit tested**: `vim` reflow on resize, `htop`
rendering correctly, and general "does this feel right interactively"
behaviour. These live in real terminal-emulator behaviour that's
impractical to assert on programmatically without writing a terminal
emulator ourselves (which is exactly the complexity Part 1 is scoped to
avoid). These were checked manually during development and are called
out here rather than papered over with a test that doesn't actually
prove anything.

Run tests with:
```
pip install pytest --break-system-packages   # if not already available
python3 -m pytest tests/ -v
```

## What's next (not done, and why)

- **Part 2 (completion notifications)** — the actual point of the full
  assignment. Not implemented in this submission. The extension point
  is already in place: `Multiplexer.observer` is called on every pane
  output event, focus change, and pane close; a real implementation
  would replace `NullObserver` with something that watches the pane's
  foreground process group (via the PTY) and timing to infer command
  completion, then surfaces a notification when the relevant pane isn't
  focused. See the parent assignment's DECISIONS.md requirements for
  what that write-up needs to cover once built.
- A persistent status bar (would need escape-sequence parsing).
- Config for prefix key / shell path via CLI flags or a config file —
  currently hardcoded, which is fine for an assignment but would be an
  early ask in real use.
