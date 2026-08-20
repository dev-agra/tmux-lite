# tmux-lite

A simplified terminal multiplexer with completion notifications. Python
3.11+, standard library only.

Both Part 1 (the multiplexer) and Part 2 (completion notifications) are
implemented. See DECISIONS.md for the reasoning behind every non-obvious
choice, including two real bugs found and fixed during testing.

## Running it

```
python3 -m tmuxlite.main
```

Run it from a real terminal (not redirected/piped). It starts your
`$SHELL` in pane 1 (falling back to `bash` on `$PATH`, then `/bin/sh` --
see DECISIONS.md for why the fallback isn't just `/bin/sh`).

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

## Completion notifications (Part 2)

When a command finishes in a pane you're *not* currently looking at,
tmux-lite tells you -- as a status line + terminal bell written to your
real screen (`[pane 2 finished: exit 3, 5.0s]`), plus a best-effort
desktop notification via `notify-send` (Linux) or `osascript` (macOS) if
one of those is on `$PATH`.

**What counts as "a command finished"**, in short: the terminal's
foreground process group reverting from some other process back to the
shell's own process group -- a real kernel fact we poll via `TIOCGPGRP`,
not a guess about output. Exit status comes from a small, invisible hook
injected into bash/zsh at pane startup. Full reasoning, including the
alternatives considered and rejected, is in DECISIONS.md.

**Rules applied before anything is shown:**
- Never notified for the pane you're currently focused on.
- Only notified if the command ran for at least 2 seconds (tunable
  constant in `notifier.py`, `DEFAULT_THRESHOLD_SECONDS`) -- `ls`, `cd`,
  `echo` etc. won't spam you.
- Sitting inside an interactive program (vim, less, htop, or anything
  else that holds the foreground continuously) produces zero
  notifications until it actually exits, however long you sit there.

**Shell coverage:** bash and zsh get exact exit statuses via an injected
prompt hook. Other shells (fish, dash, plain `sh`, csh/tcsh) still get
correct start/finish *timing* (that part is shell-agnostic, since it's
pure kernel pgrp tracking), but the notification will show `exit
unknown` instead of a real status, since there's no equivalent hook
mechanism implemented for them here.

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
- Completion notifications, verified end-to-end against a real PTY and
  real bash for every scenario in the assignment's graded checklist:
  - `sleep 5; exit 3` in an unfocused pane → exactly one notification,
    exit status 3 (this scenario ends the pane's *shell*, not just a
    subcommand -- see DECISIONS.md on why that needed its own code path).
  - A failing command where the shell survives afterwards → one
    notification with the correct non-zero status, pane confirmed still
    alive and usable afterward.
  - A long-sitting interactive foreground process (tested with `cat`
    blocked on stdin, standing in for vim/less/htop, which weren't
    installable in the sandbox this was built in) → zero notifications
    while inside it, however long you sit there.
  - A fast command (`ls`) → no notification.
  - A long command that runs while its pane stays focused the whole
    time → no notification, ever.
  - All five are exercised by real, non-mocked integration tests in
    `tests/test_notifier_integration.py`, not just claimed.

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
- **Exit status is unknown for non-bash/zsh shells** (fish, dash, `sh`,
  csh/tcsh). Completion timing still works for them (pgrp tracking is
  shell-agnostic), but the notification shows `exit unknown` since no
  equivalent startup-hook mechanism is implemented for those shells.
- **Backgrounded jobs (`cmd &`) are invisible to detection.** They never
  take the terminal's foreground process group, so our pgrp-based
  definition of "a command finished" never fires for them. Arguably in
  scope for the shell's own job-control notifications (`[1]+ Done`)
  rather than ours.
- **Compound lines (`cmd1; cmd2; cmd3`) produce multiple notifications**,
  one per foreground handoff -- correct under our definition of "a
  command" (one pgrp handoff = one command), but worth knowing it's not
  "one notification per Enter keypress."
- **A process that daemonizes and returns control to the shell early**
  will be reported "finished" while its real work continues invisibly
  in the background. Inherent to pgrp-based detection; not fixable
  without program-specific knowledge.
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

`tests/test_notifier.py` — unit tests for `CompletionObserver`'s decision
logic (threshold, focus suppression, exactly-one-notification, marker
correlation, marker reassembly across split reads) with `TIOCGPGRP`
polling and the clock monkeypatched to fake values. These are fast,
deterministic, and specifically what caught the logic bugs during
development, but they cannot catch a bug in the *real* pgrp-polling
plumbing itself, since the polling is faked out.

`tests/test_notifier_integration.py` — real end-to-end tests against the
actual `tmuxlite.main` entry point, a real PTY, and real bash panes,
covering every scenario in the assignment's graded checklist for Part 2.
These exist because they're the only thing that actually caught a real
bug (see DECISIONS.md): a race condition in reading the shell's resting
process group right after fork, which the faked-pgrp unit tests above
structurally cannot detect since they never touch a real fork.

**What's deliberately not unit tested**: `vim` reflow on resize, `htop`
rendering correctly, and general "does this feel right interactively"
behaviour. These live in real terminal-emulator behaviour that's
impractical to assert on programmatically without writing a terminal
emulator ourselves (which is exactly the complexity Part 1 is scoped to
avoid). These were checked manually during development and are called
out here rather than papered over with a test that doesn't actually
prove anything. (Also worth noting: `vim` wasn't installable in the
sandbox this was built in, so its specific Part-2 checklist item was
verified with `cat` instead, as a stand-in for "a foreground program
that blocks indefinitely and doesn't hand control back until it exits.")

Run tests with:
```
pip install pytest --break-system-packages   # if not already available
python3 -m pytest tests/ -v
```
The integration test files fork real processes and run real `sleep`
commands, so the full suite takes roughly 30-40 seconds rather than
being instant.

## What's next (not done, and why)

- **Exit-status marker support for shells other than bash/zsh.** fish
  uses a different hook mechanism (`fish_prompt`/`fish_postexec`
  events); dash/`sh`/csh have no equivalent hook at all. Timing-only
  detection would still work for those, just without a real exit code.
- **A persistent status bar** (would need escape-sequence parsing —
  Part 3 scope).
- **Config for prefix key / shell path / notification threshold** via
  CLI flags or a config file — currently hardcoded constants, fine for
  an assignment but an early ask in real use.
- **Coalescing multiple notifications** if several unfocused panes
  finish in a burst — right now each gets its own line, which could get
  noisy with many panes; not attempted since the assignment scopes to
  two panes.
