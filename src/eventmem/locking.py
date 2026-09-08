"""Cross-process compatibility locks for the legacy file store."""

from contextlib import contextmanager
import threading

_guard = threading.Lock()
_locks = {}


@contextmanager
def exclusive(path):
    import fcntl

    with _guard:
        lock = _locks.setdefault(str(path.resolve()), threading.RLock())
    with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file, fcntl.LOCK_UN)
