"""
HTTP with a hard wall-clock deadline.

requests' timeout applies between bytes, so a server that holds a connection open
or trickles data can block forever. Every outbound call goes through post()/get()
here, which abandon the request after DEADLINE seconds and raise a Timeout that
callers' existing retry logic already handles.

Each request runs on its own daemon thread. (A bounded pool doesn't work: abandoned
requests keep their worker blocked, and once every worker is stuck, new requests
queue behind them and time out forever.)
"""
import threading

import requests

DEADLINE = 120


def _with_deadline(fn, *args, deadline: float = DEADLINE, **kwargs) -> requests.Response:
    box: dict = {}

    def run():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as e:   # surfaced to the caller below
            box["error"] = e

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise requests.Timeout(f"no complete response within {deadline}s")
    if "error" in box:
        raise box["error"]
    return box["result"]


def post(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    return _with_deadline(requests.post, url, **kwargs)


def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    return _with_deadline(requests.get, url, **kwargs)
