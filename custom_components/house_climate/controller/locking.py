"""Prevent two live supervisors on the same host from fighting over the heads."""
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def live_lock(path: Path | None = None):
    try:
        import fcntl
    except ImportError:
        raise RuntimeError("Live mode currently requires Linux/macOS process locking") from None
    # Share the original lock with older processes during a branding upgrade.
    path = path or Path(tempfile.gettempdir()) / "house-climate-live.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another live climate controller holds the local process lock") from None
        yield
    finally:
        os.close(descriptor)
