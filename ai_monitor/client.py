"""Small bounded JSON requests over a private local Unix socket."""

import json
import socket

from .paths import socket_path

MAX_MESSAGE = 2 * 1024 * 1024


def request(message, timeout=4, path=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path or socket_path()))
        client.sendall(json.dumps(message).encode() + b"\n")
        with client.makefile("rb") as stream:
            raw = stream.readline(MAX_MESSAGE + 1)
        if len(raw) > MAX_MESSAGE or not raw.endswith(b"\n"):
            raise ValueError("Invalid service response")
        response = json.loads(raw)
        if not response.get("ok"):
            raise ValueError(response.get("error", "The service could not complete the request"))
        return response.get("result")
