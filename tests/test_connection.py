"""Finding IDE windows quickly: a window whose process has ended is dropped without a network probe, and the
other windows are probed at the same time (docs/design.md, "Extension protocol")."""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from autodebug.connection import discover, owner_process, process_ended


def _ended_pid() -> int:
    """The id of a process that has just exited (and whose handle is closed)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class WindowFiles(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()

    def write(self, window: str, port: int) -> Path:
        path = self.folder / f"{window}.json"
        path.write_text(json.dumps({"protocol": 4, "port": port, "token": secrets.token_hex(32), "window": window,
                                    "ide": "visualstudio", "folders": ["D:/game"], "version": "0.8.0"}))
        if os.name != "nt":
            path.chmod(0o600)
        return path

    def listening(self) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen()
        self.servers.append(server)
        return server.getsockname()[1]

    def closed_port(self) -> int:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()
        return port

    def test_the_window_process_is_read_from_the_name(self):
        self.assertEqual(owner_process("visualstudio-188ce0471cc7-16832"), 16832)
        self.assertEqual(owner_process("cursor-335a3511b449-10116"), 10116)
        self.assertIsNone(owner_process("cursor-fake-a1b2c3"))

    def test_a_running_process_has_not_ended(self):
        self.assertFalse(process_ended(os.getpid()))
        self.assertFalse(process_ended(None))
        self.assertFalse(process_ended(0))

    def test_an_exited_process_has_ended(self):
        self.assertTrue(process_ended(_ended_pid()))

    def test_a_dead_windows_file_is_removed_without_a_probe(self):
        dead = self.write(f"visualstudio-188ce0471cc7-{_ended_pid()}", self.listening())
        live = self.write(f"visualstudio-2dbdfabf254e-{os.getpid()}", self.listening())
        found = discover(self.folder)
        self.assertEqual([w.window for w in found], [live.stem])
        self.assertFalse(dead.exists(), "the file of a window whose process ended is removed")
        self.assertTrue(live.exists())

    def test_a_window_without_a_process_id_is_still_probed(self):
        self.write("cursor-fake-a1b2c3", self.listening())
        self.assertEqual(len(discover(self.folder)), 1)

    def test_unreachable_windows_are_probed_at_the_same_time(self):
        # A running process whose port is closed costs one probe; several of them must not add up.
        for index in range(6):
            self.write(f"cursor-{index:012x}-{os.getpid()}", self.closed_port())
        self.write(f"cursor-ffffffffffff-{os.getpid()}", self.listening())
        started = time.monotonic()
        found = discover(self.folder)
        self.assertEqual(len(found), 1)
        self.assertLess(time.monotonic() - started, 1.5, "six 0.5 s probes run in parallel, not one after another")


if __name__ == "__main__":
    unittest.main()
