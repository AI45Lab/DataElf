"""
sources/_http.py
================
共享 HTTP 工具（带重试）。内部使用，不对外暴露。
"""

from __future__ import annotations

import time
from typing import Any

import requests

_TIMEOUT    = 15
_MAX_RETRY  = 2
_RETRY_WAIT = 1.5


def _get(url: str, params: dict | None = None, fmt: str = "json") -> Any:
    """
    带重试的 GET 请求。

    Returns
    -------
    JSON dict / 纯文本，失败时抛出 RuntimeError。
    404 返回 None（正常"未找到"）。
    """
    headers   = {"Accept": "application/json"} if fmt == "json" else {}
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRY + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json() if fmt == "json" else resp.text
        except requests.exceptions.Timeout:
            last_exc = TimeoutError(f"Timeout after {_TIMEOUT}s: {url}")
        except requests.exceptions.HTTPError as e:
            if resp.status_code == 404:
                return None
            last_exc = e
        except Exception as e:
            last_exc = e

        if attempt < _MAX_RETRY:
            time.sleep(_RETRY_WAIT)

    raise RuntimeError(f"Request failed after {_MAX_RETRY + 1} attempts: {last_exc}")


def _post(url: str, data: dict | None = None) -> Any:
    """
    带重试的 POST 请求（application/x-www-form-urlencoded）。
    返回纯文本（InterProScan 返回 job_id 字符串）。
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            resp = requests.post(url, data=data, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
        if attempt < _MAX_RETRY:
            time.sleep(_RETRY_WAIT)
    raise RuntimeError(f"POST failed after {_MAX_RETRY + 1} attempts: {last_exc}")


def _post_form(url: str, params: dict | None = None) -> str | None:
    """
    带重试的 POST 请求（NCBI BLAST 提交格式）。
    返回纯文本响应。
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            resp = requests.post(url, data=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
        if attempt < _MAX_RETRY:
            time.sleep(_RETRY_WAIT)
    return None


def _get_text(url: str, params: dict | None = None) -> str | None:
    """
    带重试的 GET 请求，返回纯文本。失败返回 None。
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRY + 1):
        try:
            resp = requests.get(url, params=params, timeout=60)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            last_exc = e
        if attempt < _MAX_RETRY:
            time.sleep(_RETRY_WAIT)
    return None