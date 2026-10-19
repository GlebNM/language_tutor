#!/usr/bin/env python3
"""Optional foreground host relay for the app's two HTTPS origins; TLS stays end to end."""

from __future__ import annotations

import argparse
import select
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType
from typing import Any

ALLOWED_TARGETS = frozenset({"auth.openai.com:443", "api.openai.com:443"})
MAX_CONNECTIONS = 16
MAX_TUNNEL_SECONDS = 120
IDLE_SECONDS = 30


class LoopbackRelay(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = MAX_CONNECTIONS

    def __init__(self, port: int) -> None:
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.connections: set[socket.socket] = set()
        self.connection_lock = threading.Lock()
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", port), ConnectHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        with self.connection_lock:
            self.connections.add(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self.connection_lock:
                self.connections.discard(request)
            self.slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Do not print request data, headers, connection targets or handler tracebacks.
        pass

    def close_connections(self) -> None:
        self.stopping.set()
        with self.connection_lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()


class ConnectHandler(BaseHTTPRequestHandler):
    server: LoopbackRelay

    def setup(self) -> None:
        self.request.settimeout(IDLE_SECONDS)
        super().setup()

    def log_message(self, *args: object) -> None:
        pass

    def do_CONNECT(self) -> None:
        self.close_connection = True
        if self.path not in ALLOWED_TARGETS:
            self.send_error(403)
            return
        host = self.path.split(":", 1)[0]
        try:
            upstream = socket.create_connection((host, 443), timeout=10)
        except OSError:
            self.send_error(502)
            return
        with upstream:
            with self.server.connection_lock:
                self.server.connections.add(upstream)
            try:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.wfile.flush()
                self.relay(upstream)
            except OSError:
                pass
            finally:
                with self.server.connection_lock:
                    self.server.connections.discard(upstream)

    def relay(self, upstream: socket.socket) -> None:
        started = last_data = time.monotonic()
        while not self.server.stopping.is_set():
            now = time.monotonic()
            if now - started >= MAX_TUNNEL_SECONDS or now - last_data >= IDLE_SECONDS:
                return
            ready, _, _ = select.select([self.connection, upstream], [], [], 1)
            for source in ready:
                chunk = source.recv(65536)
                if not chunk:
                    return
                destination = upstream if source is self.connection else self.connection
                destination.sendall(chunk)
                last_data = time.monotonic()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766, help="Host loopback port (default: 8766)")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        server = LoopbackRelay(args.port)
    except OSError:
        parser.exit(1, "Cannot open the requested host loopback relay port.\n")
    stopped = threading.Event()

    def stop(signum: int, frame: FrameType | None) -> None:
        stopped.set()

    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, stop)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        f"OpenAI HTTPS relay ready on 127.0.0.1:{server.server_port}. Stop with Ctrl+C.", flush=True
    )
    try:
        stopped.wait()
    finally:
        server.close_connections()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        print("OpenAI HTTPS relay stopped.", flush=True)


if __name__ == "__main__":
    main()
