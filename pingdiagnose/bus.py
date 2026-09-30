import threading

_cond = threading.Condition()
_seq = 0
stopping = threading.Event()


def publish():
    global _seq
    with _cond:
        _seq += 1
        _cond.notify_all()


def current():
    return _seq


def wait(seq, timeout):
    with _cond:
        _cond.wait_for(lambda: _seq != seq or stopping.is_set(), timeout)
        return _seq


def shutdown():
    stopping.set()
    with _cond:
        _cond.notify_all()
