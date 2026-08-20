"""Entry point for the terminal multiplexer.

Wires together raw-mode terminal handling, SIGWINCH, and the event loop.
The try/finally around mux.run() is a last line of defence for cleaning
up pane processes; RawTerminal's own __exit__/atexit/signal handlers are
the last line of defence for terminal restoration, and are intentionally
independent of whether this function's own cleanup succeeds.
"""
import signal
import sys

from .multiplexer import Multiplexer
from .notifier import CompletionObserver
from .terminal import RawTerminal


def main(argv=None):
    mux = Multiplexer(observer=CompletionObserver())

    def on_winch(signum, frame):
        mux.request_resize()

    with RawTerminal():
        old_winch = signal.signal(signal.SIGWINCH, on_winch)
        try:
            mux.run()
        finally:
            mux.shutdown()
            signal.signal(signal.SIGWINCH, old_winch)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
