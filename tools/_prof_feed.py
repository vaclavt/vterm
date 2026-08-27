"""One-shot profile of the feed/parse path, to rank hotspots."""
import cProfile
import pstats
import sys
import time

sys.path.insert(0, "tools")
sys.path.insert(0, ".")
from tools.bench_vs_terminus import colour_flood, plain_flood, tui_repaint, _bootstrap  # noqa

from vterm.emulator import VtScreen, VtStream
from tests._bootstrap import FakeProcess  # noqa


def build():
    screen = VtScreen(100, 40, process=FakeProcess(), history=10000)
    return VtStream(screen)


def run(name, data):
    stream = build()
    t0 = time.perf_counter()
    for i in range(0, len(data), 32768):
        stream.feed(data[i:i + 32768])
    dt = (time.perf_counter() - t0) * 1000
    print("{:<20} {:.1f} ms".format(name, dt))


def profiled(name, data):
    stream = build()
    pr = cProfile.Profile()
    pr.enable()
    for i in range(0, len(data), 32768):
        stream.feed(data[i:i + 32768])
    pr.disable()
    s = pstats.Stats(pr)
    print("\n==== {} ====".format(name))
    s.sort_stats("cumulative").print_stats(18)


if __name__ == "__main__":
    floods = {
        "colour": colour_flood(),
        "plain": plain_flood(),
        "tui": tui_repaint(),
    }
    for n, d in floods.items():
        run(n, d)
    profiled("colour", colour_flood())
    profiled("tui", tui_repaint())
