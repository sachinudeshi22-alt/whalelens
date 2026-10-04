"""
HTTP with a hard wall-clock deadline.

requests' timeout applies between bytes, so a server that holds a connection open
or trickles data can block forever. Every outbound call goes through post()/get()
here, which abandon the request after DEADLINE seconds and raise a Timeout that
callers' existing retry logic already handles.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import requests

DEADLINE = 120
_pool = ThreadPoolExecutor(max_workers=4)


def _with_deadline(fn, *args, deadline: float = DEADLINE, **kwargs) -> requests.Response:
    future = _pool.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=deadline)
    except FutureTimeout:
        future.cancel()
        raise requests.Timeout(f"no complete response within {deadline}s")


def post(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    return _with_deadline(requests.post, url, **kwargs)


def get(url: str, **kwargs) -> requests.Response:
    kwargs.setdefault("timeout", 60)
    return _with_deadline(requests.get, url, **kwargs)
