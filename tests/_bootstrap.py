""" sys.path setup and fakes shared by the test scripts

They run as plain scripts (see README), so each imports this first to get the
sublime stubs and the Package Control libraries on the path.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ST_LIB = os.path.expanduser(
    "~/Library/Application Support/Sublime Text/Lib/python38")

for path in (ROOT, ST_LIB, os.path.join(ROOT, "tests", "stubs")):
    if path not in sys.path:
        sys.path.insert(0, path)


class FakeProcess:
    """Captures what the emulator writes to the pty, one entry per write."""

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    @property
    def written(self):
        return "".join(self.writes)

    @written.setter
    def written(self, value):
        self.writes = [value] if value else []
