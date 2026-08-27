"""Micro-bench the per-char steps of VtScreen.draw."""
import os
import time

import sys
ST_LIB = os.path.expanduser("~/Library/Application Support/Sublime Text/Lib/python38")
sys.path.insert(0, ST_LIB)
sys.path.insert(0, "tests/stubs")
from pyte.screens import wcwidth as pyte_wcwidth  # lru_cache'd
from wcwidth import wcwidth as raw_wcwidth
from functools import lru_cache

data = ("x" * 50 + "\x1b[38;5;100m") * 40000  # ~1.2M printable chars


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


t("iter only", loop_iter_only)
t("wcwidth (pyte lru_cache)", loop_wcwidth_pyte)
t("wcwidth (raw)", loop_wcwidth_raw)
t("min(x+w, cols) per char", loop_min)
t("min -> branch per char", loop_min_vs_cmp)
t("dict get/miss/set per char", loop_dict_get_set)
