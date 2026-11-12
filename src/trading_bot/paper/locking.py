"""One operating-system writer lease per persistent paper account."""

from pathlib import Path
import errno
import os


class PaperAccountInUseError(RuntimeError):
    """Another worker owns the same paper account."""


def writer_active(journal_path):
    """Return True/False/unknown without creating a journal or lock file."""
    path = Path(journal_path).expanduser().resolve()
    lock_path = path.with_name(path.name + ".writer.lock")
    try:
        if not lock_path.is_file():
            return False
        with lock_path.open("r+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return True
                return None
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return None


class JournalWriterLock:
    """Hold an advisory OS lock until close; a dead process releases it.

    The lock file remains on disk deliberately. Deleting it during release can
    let another process lock a different inode for the same account pathname.
    Readers do not acquire this lease. SQLite files must be on a local filesystem
    with working OS file locking; this is not a distributed account coordinator.
    """

    def __init__(self, journal_path):
        self.journal_path = Path(journal_path).expanduser().resolve()
        self.path = self.journal_path.with_name(self.journal_path.name + ".writer.lock")
        self._handle = None

    def acquire(self):
        if self._handle is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise PaperAccountInUseError(
                "This paper account is already in use. Stop its other worker before starting another."
            ) from exc
        self._handle = handle
        return self

    def close(self):
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.close()

