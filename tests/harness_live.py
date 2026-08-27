""" Live harness: run a real command inside VtScreen/VtStream without Sublime.
Usage: python3 tests/harness_live.py <cmd> [seconds] [--send <text> | --type <text>]
--send writes the text in one go; --type writes a character per write, as typing does. """
import codecs
import os
import sys
import threading
import time

import _bootstrap  # sets sys.path for the imports below

from vterm.emulator import VtScreen, VtStream
from vterm.terminal import TerminalPtyProcess, agent_env

cmd = sys.argv[1:2] or ["/bin/zsh"]
seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
send = None
if "--send" in sys.argv:
    send = sys.argv[sys.argv.index("--send") + 1]
typed = None
if "--type" in sys.argv:
    typed = sys.argv[sys.argv.index("--type") + 1]

# the same process and environment the plugin spawns
proc = TerminalPtyProcess.spawn(
    cmd, cwd=os.getcwd(), env=agent_env(), dimensions=(30, 100))

replies = []


class Screen(VtScreen):
    """Records the query replies the emulator writes back to the app."""

    def write_process_input(self, data):
        replies.append(data)
        super().write_process_input(data)


screen = Screen(100, 30, process=proc)
stream = VtStream(screen)

decoder = codecs.getincrementaldecoder("utf-8")("replace")
lock = threading.Lock()
stop = [False]


def reader():
    while not stop[0]:
        try:
            raw = proc.read(4096)
        except EOFError:
            break
        with lock:
            stream.feed(decoder.decode(raw))


t = threading.Thread(target=reader, daemon=True)
t.start()

if send is not None:
    time.sleep(2)
    proc.write(send)

if typed is not None:
    time.sleep(2)
    # one write per character: the app frames its input per read, so a whole
    # string in one write is not the same test as typing it
    for char in typed:
        proc.write(char)
        time.sleep(0.04)

time.sleep(seconds)
stop[0] = True
try:
    proc.terminate(force=True)
except Exception:
    pass

print("=== queries answered ===")
for r in replies:
    print(repr(r))
print("=== screen (non-empty rows) ===")
for y in range(screen.lines):
    row = "".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()
    if row:
        print("{:2}| {}".format(y, row))
print("=== modes: {} title: {!r} alt: {}".format(
    sorted(m >> 5 for m in screen.mode if m >= 32), screen.title,
    screen.alternate_buffer_mode))
