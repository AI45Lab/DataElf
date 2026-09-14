"""One bounded JSON request per process; SDK diagnostics never reach the agent."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Iterator

from .adapter import ServingAdapter, create_client, validate

MAX_INPUT = 8192


@contextlib.contextmanager
def mute_sdk() -> Iterator[None]:
    # Redirect OS descriptors too: native DLDB and logging handlers may bypass Python streams.
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(fd) for fd in (1, 2)]
    try:
        with open(os.devnull, "w") as sink:
            for fd in (1, 2):
                os.dup2(sink.fileno(), fd)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        for fd, original in zip((1, 2), saved):
            os.dup2(original, fd)
            os.close(original)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("request too large")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) != {"tool", "arguments"}:
            raise ValueError("invalid envelope")
        validate(request["tool"], request["arguments"])
        with mute_sdk():
            client = create_client()
            try:
                result = ServingAdapter(client).call(request["tool"], request["arguments"])
            finally:
                client.close()
        envelope = {"isError": False, "result": result}
    except Exception:  # noqa: BLE001 - fail closed without exposing SDK diagnostics
        # Never stringify SDK exceptions, validation input, endpoints or credentials.
        envelope = {"isError": True, "result": {"error": "WT_READ_FAILED"}}
    sys.stdout.write(json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
