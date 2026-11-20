"""Serve an ASGI application on an ephemeral loopback port for HTTP tests."""

from contextlib import contextmanager
import socket
from threading import Thread
from time import monotonic, sleep

import uvicorn


@contextmanager
def serve(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
        thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started:
                if not thread.is_alive() or monotonic() > deadline:
                    raise RuntimeError("Test HTTP server failed to start.")
                sleep(0.01)
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(10)
            if thread.is_alive():
                raise RuntimeError("Test HTTP server failed to stop.")
