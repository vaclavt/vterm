"""Profile the feed/parse path, then micro-bench the per-char steps of VtScreen.draw.

cProfile ranks the hotspots on the bench workloads; the micro loops put numbers
on the per-char candidates inside the draw path.

Run with: python3 benchmarks/profiling.py
"""
import cProfile
import os
import pstats
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import _bootstrap  # noqa: F401  sets sys.path for the imports below

from bench_vs_terminus import colour_flood, plain_flood, tui_repaint  # noqa: E402
from _bootstrap import FakeProcess  # noqa: E402
from pyte.screens import wcwidth as pyte_wcwidth  # noqa: E402  lru_cache'd
from wcwidth import wcwidth as raw_wcwidth  # noqa: E402

from vterm.emulator import VtScreen, VtStream  # noqa: E402


# ------------------------------------------------------------ feed hotspots

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


# ------------------------------------------- per-char micro-benches of draw

data = ("x" * 50 + "\x1b[38;5;100m") * 40000  # ~2M printable chars


def t(label, fn, n=3):
    best = 1e9
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    print("{:<42} {:8.3f} ms".format(label, best * 1000))


def loop_wcwidth_pyte():
    for c in data:
        w = pyte_wcwidth(c)


def loop_wcwidth_raw():
    for c in data:
        w = raw_wcwidth(c)


def loop_iter_only():
    for c in data:
        pass


def loop_min():
    x = 0
    for c in data:
        x = min(x + 1, 80)


def loop_dict_get_set():
    d = {}
    line = {}
    for i, c in enumerate(data):
        v = d.get(c)
        if v is None:
            d[c] = c
        line[i % 80] = c


def loop_min_vs_cmp():
    x = 0
    cols = 80
    for c in data:
        nx = x + 1
        x = cols if nx > cols else nx


def main():
    floods = {
        "colour": colour_flood(),
        "plain": plain_flood(),
        "tui": tui_repaint(),
    }
    for n, d in floods.items():
        run(n, d)
    profiled("colour", colour_flood())
    profiled("tui", tui_repaint())

    print("==== per-char micro-benches ====")
    t("iter only", loop_iter_only)
    t("wcwidth (pyte lru_cache)", loop_wcwidth_pyte)
    t("wcwidth (raw)", loop_wcwidth_raw)
    t("min(x+w, cols) per char", loop_min)
    t("min -> branch per char", loop_min_vs_cmp)
    t("dict get/miss/set per char", loop_dict_get_set)


if __name__ == "__main__":
    main()
