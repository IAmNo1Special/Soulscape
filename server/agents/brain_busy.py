import threading

_lock = threading.Lock()
_busy: set[str] = set()


def acquire(soul_id: str) -> bool:
    with _lock:
        if soul_id in _busy:
            return False
        _busy.add(soul_id)
        return True


def release(soul_id: str) -> None:
    with _lock:
        _busy.discard(soul_id)


def is_busy(soul_id: str) -> bool:
    with _lock:
        return soul_id in _busy


def reset() -> None:
    with _lock:
        _busy.clear()
