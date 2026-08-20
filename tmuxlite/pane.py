"""A single pane: one PTY plus one shell process.

Design decision (see DECISIONS.md): we use pty.fork() rather than hand-
rolling os.fork() + os.setsid() + TIOCSCTTY ourselves. pty.fork() already
puts the child in its own session with the slave PTY as its controlling
terminal, which is exactly the "session leader" requirement called out in
the assignment brief -- reimplementing it by hand would just reproduce
stdlib code with more chances to get the setsid/TIOCSCTTY ordering wrong.

Part 2 addition: optionally inject a small shell-startup hook (bash
PROMPT_COMMAND / zsh precmd) that reports each foreground command's exit
status via an invisible marker written into the pane's own output. This
is necessary because we are not the parent of the commands the shell
runs -- the shell reaps its own foreground children directly -- so there
is no other way to learn an exit code without the shell's cooperation.
See DECISIONS.md for the shells this covers and what happens for others.
"""
import os
import pty
import shutil
import signal
import tempfile

from . import terminal


def _default_shell():
    """Resolve which shell to run when the caller doesn't specify one.

    Prefers $SHELL (the normal case on a real login session). Falls back
    to bash if it's on PATH, rather than going straight to /bin/sh --
    on many systems /bin/sh is dash or another POSIX-minimal shell with
    no PROMPT_COMMAND/precmd mechanism, which would silently disable the
    exit-status marker this module relies on for Part 2. /bin/sh is the
    last resort only if nothing better is found.
    """
    shell = os.environ.get("SHELL")
    if shell:
        return shell
    bash = shutil.which("bash")
    if bash:
        return bash
    return "/bin/sh"


# Bash: source the user's real ~/.bashrc first (so their prompt/aliases/
# env still apply), then prepend our hook onto PROMPT_COMMAND so it runs
# before anything the user's own PROMPT_COMMAND might do (which matters
# because $? must still hold the *command's* exit status when our hook
# reads it -- if we ran after the user's hooks, one of their commands
# could have already overwritten $?).
_BASH_RC_TEMPLATE = """\
if [ -f "$HOME/.bashrc" ]; then
    source "$HOME/.bashrc"
fi

__tmuxlite_report_status() {{
    printf '\\033]{marker_id};%s\\007' "$?"
}}

case "$PROMPT_COMMAND" in
    *__tmuxlite_report_status*) ;;
    *) PROMPT_COMMAND="__tmuxlite_report_status${{PROMPT_COMMAND:+; $PROMPT_COMMAND}}" ;;
esac
"""

# zsh: same idea via ZDOTDIR + precmd_functions. Note this means a real
# ~/.zshenv (which zsh reads independently of ZDOTDIR-relative .zshrc in
# some configurations) may be bypassed -- documented as a known
# limitation rather than something this handles perfectly.
_ZSH_RC_TEMPLATE = """\
if [ -f "$HOME/.zshrc" ]; then
    source "$HOME/.zshrc"
fi

__tmuxlite_report_status() {{
    printf '\\033]{marker_id};%s\\007' "$?"
}}

precmd_functions=(__tmuxlite_report_status "${{precmd_functions[@]}}")
"""

MARKER_ID = "9278"  # arbitrary, unregistered OSC-style id -- see notifier.py


class Pane:
    def __init__(self, pane_id, shell=None, rows=24, cols=80, report_exit_status=True):
        self.pane_id = pane_id
        self.shell = shell or _default_shell()
        self.rows = rows
        self.cols = cols
        self.pid = None
        self.master_fd = None
        self.alive = False
        self.exit_status = None
        self.report_exit_status = report_exit_status
        self._tmp_paths = []  # rc file/dir paths to clean up on close

    def start(self):
        pid, master_fd = pty.fork()
        if pid == 0:
            # Child: stdio is now the slave PTY, and pty.fork() has
            # already made us a session leader with it as our controlling
            # terminal. Build the argv (possibly with a shell-hook rc
            # file) and exec.
            argv = self._build_argv()
            os.execvp(argv[0], argv)
            os._exit(127)  # only reached if execvp itself fails
        self.pid = pid
        self.master_fd = master_fd
        self.alive = True
        terminal.set_winsize(self.master_fd, self.rows, self.cols)
        return self

    def _build_argv(self):
        """Choose how to launch the shell. Only called in the forked
        child, right before exec -- writing temp files here is safe since
        the child is about to replace its own image anyway."""
        shell_name = os.path.basename(self.shell)
        if not self.report_exit_status:
            return [self.shell]
        try:
            if shell_name == "bash":
                return self._bash_argv()
            if shell_name == "zsh":
                return self._zsh_argv()
        except OSError:
            pass  # fall through to plain exec if writing the rc file fails
        return [self.shell]

    def _bash_argv(self):
        fd, path = tempfile.mkstemp(prefix="tmuxlite-bashrc-")
        with os.fdopen(fd, "w") as f:
            f.write(_BASH_RC_TEMPLATE.format(marker_id=MARKER_ID))
        self._tmp_paths.append(path)
        return [self.shell, "--rcfile", path, "-i"]

    def _zsh_argv(self):
        zdotdir = tempfile.mkdtemp(prefix="tmuxlite-zdotdir-")
        with open(os.path.join(zdotdir, ".zshrc"), "w") as f:
            f.write(_ZSH_RC_TEMPLATE.format(marker_id=MARKER_ID))
        self._tmp_paths.append(zdotdir)
        os.environ["ZDOTDIR"] = zdotdir  # only affects this (about to be
                                          # replaced) child process's env
        return [self.shell, "-i"]

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
        self._cleanup_tmp_paths()

    def _cleanup_tmp_paths(self):
        import shutil
        for path in self._tmp_paths:
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.unlink(path)
            except OSError:
                pass
        self._tmp_paths = []

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
