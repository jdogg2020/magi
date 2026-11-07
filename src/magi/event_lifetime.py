"""Shared admission/deletion boundary for event post-processing in this process."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import threading

LOCK = threading.RLock()
IN_USE = set()


def serialized(function):
    @wraps(function)
    def call(*args, **kwargs):
        with LOCK:
            return function(*args, **kwargs)
    return call


def busy(directory):
    with LOCK:
        return str(Path(directory).absolute()) in IN_USE


@contextmanager
def pin(directory, validate):
    key = str(Path(directory).absolute())
    with LOCK:
        if key in IN_USE:
            raise BlockingIOError('event processing already admitted')
        validate()
        IN_USE.add(key)
    try:
        yield
    finally:
        with LOCK:
            IN_USE.discard(key)


class Postprocessing:
    """One station-wide expensive slot; alternate recovery slices and waiting stills."""
    def __init__(self):
        self.condition = threading.Condition()
        self.active = False
        self.media_waiting = self.recovery_waiting = 0
        self.previous = 'recovery'

    @contextmanager
    def acquire(self, kind, cancelled=lambda: False):
        attr = kind+'_waiting'
        with self.condition:
            setattr(self, attr, getattr(self, attr)+1)
            try:
                while self.active or (self.media_waiting and self.recovery_waiting and kind == self.previous):
                    if cancelled():
                        raise InterruptedError('postprocessing cancelled')
                    self.condition.wait(.05)
                if cancelled():
                    raise InterruptedError('postprocessing cancelled')
                self.active = True
            finally:
                setattr(self, attr, getattr(self, attr)-1)
        try:
            yield
        finally:
            with self.condition:
                self.active = False
                self.previous = kind
                self.condition.notify_all()


POSTPROCESSING = Postprocessing()


def reserved(directory):
    with LOCK:
        base=Path(directory).absolute()
        return 256*1024*1024*sum(Path(p).is_relative_to(base) for p in IN_USE)
