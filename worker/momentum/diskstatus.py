"""Read-only disk and journal-size status for the persistent volume (no secrets, numbers only)."""
from pathlib import Path
import shutil


def _size(path):
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def disk_status(db_path, *, min_free_bytes=0):
    """Return free/total bytes of the volume holding ``db_path`` and the SQLite file sizes.

    Never raises: an unreadable volume reports ``available: False``. ``low`` is True when
    free space is below ``min_free_bytes`` (0 disables the comparison).
    """
    base = str(db_path)
    out = {"available": False, "journal_bytes": _size(base), "wal_bytes": _size(base + "-wal")}
    try:
        usage = shutil.disk_usage(Path(base).parent)
    except OSError:
        return out
    used_pct = round(100.0 * (usage.total - usage.free) / usage.total, 2) if usage.total else None
    out.update(available=True, free_bytes=usage.free, total_bytes=usage.total, used_percent=used_pct,
               min_free_bytes=int(min_free_bytes), low=bool(min_free_bytes > 0 and usage.free < min_free_bytes))
    return out
