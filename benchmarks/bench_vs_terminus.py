""" Render-cost comparison between vterm and Terminus

Both plugins turn pty output into view text plus add_regions overlays, so what
they cost the editor is the number of Sublime calls a frame makes -- the plugin
host is a separate process (see Architecture.md). This drives both render
commands against a fake view that counts every call, on the same input, with
each plugin's own render gate deciding when a frame happens.

Terminus is read straight out of its .sublime-package by zipimport; it is
skipped when not installed. Parse times are real, call counts stand in for the
cross-process cost, which a fake view cannot show.

Run with: python3 benchmarks/bench_vs_terminus.py
"""
import collections
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
import _bootstrap  # noqa: F401  sets sys.path for the imports below

TERMINUS = os.path.expanduser(
    "~/Library/Application Support/Sublime Text/Installed Packages/Terminus.sublime-package")

import sublime

ROWS, COLUMNS = 40, 100
SCROLLBACK = 10000


class FakeSelection(list):
    def clear(self):
        del self[:]

    def add(self, region):
        self.append(region)


class CountingView:
    """A view that answers with real line arithmetic and tallies every call."""

    _ids = [0]

    def __init__(self, counter):
        CountingView._ids[0] += 1
        self._id = CountingView._ids[0]
        self.c = counter
        self.text = ""
        self.selection = FakeSelection()
        self.regions = {}
        self.folds = []
        self._name = ""
        self._settings = sublime.Settings()
        # so a plugin that renders through a second command has it counted too
        self.commands = {}

    def _t(self, name):
        self.c[name] += 1

    def id(self):
        return self._id

    def size(self):
        self._t("size")
        return len(self.text)

    def rowcol(self, point):
        self._t("rowcol")
        head = self.text[:point]
        return (head.count("\n"), point - (head.rfind("\n") + 1))

    def text_point(self, row, col):
        self._t("text_point")
        lines = self.text.split("\n")
        start = sum(len(l) + 1 for l in lines[:row])
        return start + min(col, len(lines[row]) if row < len(lines) else 0)

    def _line(self, point):
        start = self.text.rfind("\n", 0, point) + 1
        end = self.text.find("\n", point)
        return sublime.Region(start, len(self.text) if end == -1 else end)

    def line(self, point):
        self._t("line")
        return self._line(point)

    def full_line(self, point):
        self._t("full_line")
        region = self._line(point)
        return sublime.Region(region.begin(), min(region.end() + 1, len(self.text)))

    def lines(self, region):
        self._t("lines")
        out = []
        point = region.begin()
        while point <= region.end():
            line = self._line(point)
            out.append(line)
            point = line.end() + 1
        return out

    def substr(self, region):
        self._t("substr")
        return self.text[region.begin():region.end()]

    def erase(self, edit, region):
        self._t("erase")
        self.text = self.text[:region.begin()] + self.text[region.end():]

    def replace(self, edit, region, value):
        self._t("replace")
        self.text = self.text[:region.begin()] + value + self.text[region.end():]

    def insert(self, edit, point, value):
        self._t("insert")
        self.text = self.text[:point] + value + self.text[point:]
        return len(value)

    def fold(self, region):
        self._t("fold")
        self.folds.append((region.begin(), region.end()))
        return True

    def unfold(self, region):
        self._t("unfold")
        folded, self.folds = self.folds, []
        return folded

    def folded_regions(self):
        self._t("folded_regions")
        return [sublime.Region(a, b) for a, b in self.folds]

    def add_regions(self, key, regions, scope="", *args, **kwargs):
        self._t("add_regions")
        # what crosses the process boundary is the regions, not just the call
        self.c["regions.sent"] += len(regions)
        self.c["regions.sent.merged" if "@" in key else "regions.sent.row"] += len(regions)
        self.regions[key] = (scope, [(r.begin(), r.end()) for r in regions])

    def get_regions(self, key):
        self._t("get_regions")
        _, spans = self.regions.get(key, ("", []))
        self.c["regions.read"] += len(spans)
        return [sublime.Region(a, b) for a, b in spans]

    def erase_regions(self, key):
        self._t("erase_regions")
        self.regions.pop(key, None)

    def settings(self):
        self._t("settings")
        return CountingSettings(self._settings, self.c)

    def viewport_position(self):
        self._t("viewport_position")
        return (0, 0)

    def viewport_extent(self):
        self._t("viewport_extent")
        return (800, ROWS * 20)

    def layout_extent(self):
        self._t("layout_extent")
        return (800, len(self.text))

    def text_to_layout(self, point):
        self._t("text_to_layout")
        return (0, point)

    def set_viewport_position(self, position, animate=True):
        self._t("set_viewport_position")

    def line_height(self):
        self._t("line_height")
        return 20

    def em_width(self):
        self._t("em_width")
        return 8

    def name(self):
        self._t("name")
        return self._name

    def set_name(self, name):
        self._t("set_name")
        self._name = name

    def window(self):
        self._t("window")
        return object()

    def sel(self):
        self._t("sel")
        return self.selection

    def run_command(self, name, args=None):
        self._t("run_command:" + name)
        command = self.commands.get(name)
        if command:
            command.run(None, **(args or {}))


class CountingSettings:
    """Settings reads and writes are round trips too, and Terminus does one per run."""

    def __init__(self, settings, counter):
        self._settings = settings
        self._c = counter

    def get(self, key, default=None):
        self._c["settings.get"] += 1
        return self._settings.get(key, default)

    def set(self, key, value):
        self._c["settings.set"] += 1
        self._settings[key] = value

    def erase(self, key):
        self._c["settings.erase"] += 1
        self._settings.pop(key, None)

    def has(self, key):
        return key in self._settings

    def add_on_change(self, *args):
        pass

    def clear_on_change(self, *args):
        pass


class FakeProcess:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(data)


# ------------------------------------------------------------------ workloads

def colour_flood(lines=2000, runs=6, palette=200):
    """Coloured output: `runs` SGR changes per line, drawn from `palette` colours."""
    width = COLUMNS // runs
    out = []
    for i in range(lines):
        parts = []
        for r in range(runs):
            parts.append("\x1b[38;5;{}m".format(16 + (i * runs + r) % palette))
            parts.append(str((i + r) % 10) * width)
        parts.append("\x1b[0m\r\n")
        out.append("".join(parts))
    return "".join(out)


def plain_flood(lines=2000):
    return "".join("{:04d} ".format(i) + "x" * (COLUMNS - 5) + "\r\n" for i in range(lines))


def tui_repaint(frames=60):
    """A full-screen coloured app redrawing itself; one cell differs per frame."""
    out = []
    for f in range(frames):
        parts = ["\x1b[H"]
        for row in range(ROWS):
            for r in range(4):
                parts.append("\x1b[38;5;{}m".format(16 + (row * 4 + r) % 200))
                parts.append(str((row + r) % 10) * (COLUMNS // 4))
            parts.append("\x1b[0m")
            if row < ROWS - 1:
                parts.append("\r\n")
        parts.append("\x1b[1;1H\x1b[38;5;99m{:04d}\x1b[0m".format(f))
        out.append("".join(parts))
    return "".join(out)


# ------------------------------------------------------------------- drivers

def drive(feed, need_to_render, render, data, tick):
    """Feed `data` in tick-sized reads, rendering whenever the plugin asks to."""
    parse = 0.0
    frames = 0
    for i in range(0, len(data), tick):
        start = time.perf_counter()
        feed(data[i:i + tick])
        parse += time.perf_counter() - start
        if need_to_render():
            render()
            frames += 1
    return parse, frames


def run_vterm(data, tick):
    from vterm.emulator import VtScreen, VtStream
    from vterm.render import VtermRenderCommand
    from vterm.terminal import Terminal

    counter = collections.Counter()
    view = CountingView(counter)
    terminal = Terminal(view)
    terminal.scrollback_size = SCROLLBACK
    terminal.process = FakeProcess()
    terminal.screen = VtScreen(COLUMNS, ROWS, process=terminal.process, history=SCROLLBACK)
    Terminal._terminals[view.id()] = terminal
    stream = VtStream(terminal.screen)
    command = VtermRenderCommand(view)

    def render():
        counter["run_command:vterm_render"] += 1
        command.run(None)

    try:
        parse, frames = drive(stream.feed, terminal._need_to_render, render, data, tick)
    finally:
        del Terminal._terminals[view.id()]
    return counter, parse, frames, len(view.regions)


def run_terminus(data, tick):
    from terminus.ptty import TerminalScreen, TerminalStream
    from terminus.render import TerminusRenderCommand, TerminusShowCursorCommand
    from terminus.terminal import Terminal

    counter = collections.Counter()
    view = CountingView(counter)
    terminal = Terminal(view)
    terminal.detached = False
    terminal.offset = 0
    terminal.title = None
    terminal.default_title = "Terminus"
    terminal.screen = TerminalScreen(
        COLUMNS, ROWS, process=FakeProcess(), clear_callback=lambda: None,
        reset_callback=lambda: None, history=SCROLLBACK)
    Terminal._terminals[view.id()] = terminal
    stream = TerminalStream(terminal.screen)
    command = TerminusRenderCommand(view)
    command.scrollback_history_size = SCROLLBACK
    view.commands["terminus_show_cursor"] = TerminusShowCursorCommand(view)

    def render():
        counter["run_command:terminus_render"] += 1
        command.run(None)
        # terminus clears dirty in its render loop, not in the command
        terminal.screen.dirty.clear()

    try:
        parse, frames = drive(stream.feed, terminal._need_to_render, render, data, tick)
    finally:
        del Terminal._terminals[view.id()]
    return counter, parse, frames, len(view.regions)


# -------------------------------------------------------------------- report

def measure(runner, data, tick):
    counter, parse, frames, keys = runner(data, tick)
    calls = sum(v for k, v in counter.items() if not k.startswith("regions."))
    return {
        "parse": parse * 1000,
        "frames": frames,
        "calls": calls,
        "per_frame": calls / max(frames, 1),
        "sent": counter["regions.sent"],
        "sent_row": counter["regions.sent.row"],
        "sent_merged": counter["regions.sent.merged"],
        "read": counter["regions.read"],
        "keys": keys,
        "counter": counter,
    }


ROW_FORMAT = "  {:<26}{:>14}{:>14}{:>10}"


def report(title, data, tick, both):
    print("=" * 66)
    print("{}\n  {:.0f} KiB fed in {} B reads".format(title, len(data) / 1024, tick))
    print("=" * 66)
    ours = measure(run_vterm, data, tick)
    theirs = measure(run_terminus, data, tick) if both else None

    def line(label, key, fmt="{:.0f}"):
        mine = fmt.format(ours[key])
        if not theirs:
            print(ROW_FORMAT.format(label, mine, "-", "-"))
            return
        other = fmt.format(theirs[key])
        ratio = theirs[key] / ours[key] if ours[key] else 0
        print(ROW_FORMAT.format(label, mine, other, "{:.1f}x".format(ratio)))

    print(ROW_FORMAT.format("", "vterm", "Terminus", "ratio"))
    line("parse ms", "parse", "{:.1f}")
    line("frames", "frames")
    line("sublime calls", "calls")
    line("calls / frame", "per_frame", "{:.1f}")
    line("regions sent", "sent")
    line("  of them merges", "sent_merged")
    line("regions read back", "read")
    line("live region keys", "keys")
    top = sorted(((v, k) for k, v in ours["counter"].items() if not k.startswith("regions.")),
                 reverse=True)[:6]
    print("  vterm top: " + ", ".join("{}={}".format(k, v) for v, k in top))
    if theirs:
        top = sorted(((v, k) for k, v in theirs["counter"].items() if not k.startswith("regions.")),
                     reverse=True)[:6]
        print("  Terminus top: " + ", ".join("{}={}".format(k, v) for v, k in top))
    print()


def main():
    from vterm.terminal import FEED_LIMIT

    both = os.path.exists(TERMINUS)
    if both:
        sys.path.insert(0, TERMINUS)
    else:
        print("Terminus is not installed; reporting vterm alone.\n")

    burst = FEED_LIMIT      # the most one renderer tick parses during a flood
    trickle = 512           # a few rows a frame, as a build log arrives

    report("A. colour flood, 6 runs/line, 200 colours",
           colour_flood(), burst, both)
    report("B. colour flood, 6 runs/line, 8 colours",
           colour_flood(palette=8), burst, both)
    report("C. colour trickle, a few rows a frame",
           colour_flood(lines=600), trickle, both)
    report("D. plain flood, no colour",
           plain_flood(), burst, both)
    report("E. TUI repaint, 60 full-screen frames, one cell changes",
           tui_repaint(), burst, both)


if __name__ == "__main__":
    main()
