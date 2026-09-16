""" Render-layer tests against a fake view.

The live harness exercises the emulator only, so regressions in render.py used
to reach Sublime unnoticed. Run with: python3 tests/test_render.py
"""
import _bootstrap  # sets sys.path for the imports below
from _bootstrap import FakeProcess

import sublime
from vterm.commands import VtermKeypressCommand
from vterm.emulator import VtScreen, VtStream
from vterm.mouse import VtermMouseClickCommand, VtermScrollCommand
from vterm.render import (
    CHUNK,
    VtermRenderCommand,
    focus_cursor,
    following,
    scroll_to_cursor,
)
from vterm.terminal import Terminal
from vterm.utils import SETTINGS_FILE


LINE_HEIGHT = 20


class FakeSelection(list):
    def clear(self):
        del self[:]

    def add(self, region):
        self.append(region)


class FakeView:
    """Enough of the view API for the trim paths, with real line arithmetic."""

    def __init__(self, text=""):
        self.text = text
        self.selection = FakeSelection()
        self.regions = {}
        self.commands = []
        self.folds = []
        self._name = ""
        self.viewport = (0, 0)
        self.viewport_rows = 5

    def size(self):
        return len(self.text)

    def rowcol(self, point):
        head = self.text[:point]
        row = head.count("\n")
        return (row, point - (head.rfind("\n") + 1))

    def text_point(self, row, col):
        lines = self.text.split("\n")
        start = sum(len(l) + 1 for l in lines[:row])
        return start + min(col, len(lines[row]) if row < len(lines) else 0)

    def line(self, point):
        start = self.text.rfind("\n", 0, point) + 1
        end = self.text.find("\n", point)
        return sublime.Region(start, len(self.text) if end == -1 else end)

    def full_line(self, point):
        region = self.line(point)
        return sublime.Region(region.begin(),
                              min(region.end() + 1, len(self.text)))

    def lines(self, region):
        out = []
        point = region.begin()
        while point <= region.end():
            line = self.line(point)
            out.append(line)
            point = line.end() + 1
        return out

    def substr(self, region):
        return self.text[region.begin():region.end()]

    def erase(self, edit, region):
        self.text = self.text[:region.begin()] + self.text[region.end():]

    def replace(self, edit, region, value):
        self.text = self.text[:region.begin()] + value + self.text[region.end():]
        # sublime maps a caret inside the replaced span onto the new text,
        # which is where the artefact selection came from
        for i, sel in enumerate(list(self.selection)):
            if sel.empty() and region.begin() <= sel.begin() <= region.end():
                self.selection[i] = sublime.Region(region.begin(),
                                                   region.begin() + len(value))

    def insert(self, edit, point, value):
        self.text = self.text[:point] + value + self.text[point:]
        return len(value)

    def fold(self, region):
        self.folds.append((region.begin(), region.end()))
        return True

    def unfold(self, region):
        unfolded = self.folds
        self.folds = []
        return unfolded

    def folded_regions(self):
        return [sublime.Region(b, e) for b, e in self.folds]

    def erase_regions(self, key):
        self.regions.pop(key, None)

    def add_regions(self, key, regions, scope):
        self.regions[key] = (scope, [(r.begin(), r.end()) for r in regions])

    def get_regions(self, key):
        _, pairs = self.regions.get(key, ("", []))
        return [sublime.Region(a, b) for a, b in pairs]

    def settings(self):
        return sublime.load_settings("fake")

    def viewport_position(self):
        return self.viewport

    def set_viewport_position(self, xy, animate=True):
        # scroll_past_end is off, so sublime clamps to the layout
        maxy = max(0, self.layout_extent()[1] - self.viewport_extent()[1])
        self.viewport = (xy[0], max(0, min(xy[1], maxy)))

    def line_height(self):
        return LINE_HEIGHT

    # one layout row per line; the viewport shows viewport_rows of them
    def layout_extent(self):
        return (0, (self.text.count("\n") + 1) * LINE_HEIGHT)

    def viewport_extent(self):
        return (0, self.viewport_rows * LINE_HEIGHT)

    def text_to_layout(self, point):
        return (0, self.rowcol(point)[0] * LINE_HEIGHT)

    def name(self):
        return self._name

    def set_name(self, name):
        self._name = name

    def run_command(self, name, args=None):
        self.commands.append(name)

    def sel(self):
        return self.selection


def make_terminal(rows=10, cols=20, scrollback=None):
    # real __init__ for the plain attributes; process/screen stand in for start()
    terminal = Terminal(FakeView())
    if scrollback is not None:
        terminal.scrollback_size = scrollback
    terminal.process = FakeProcess()
    terminal.screen = VtScreen(cols, rows, process=terminal.process)
    return terminal, VtStream(terminal.screen)


def make_command(view):
    # the real __init__, so the fake cannot drift from the command's attributes
    return VtermRenderCommand(view)


# an app on the alternate screen must not have its view trimmed away: the
# emulator's history deque is maxlen=0 there, so it cannot be the row cap
terminal, stream = make_terminal(rows=10)
stream.feed("\x1b[?1049h")
assert terminal.screen.alternate_buffer_mode
assert terminal.screen.history.maxlen == 0, "alt screen zeroes the deque"

terminal.view = FakeView("\n".join("row {}".format(i) for i in range(10)))
command = make_command(terminal.view)
before = terminal.view.text
command.trim_history(None, terminal)
assert terminal.view.text == before, \
    "alt screen view was trimmed: {!r}".format(terminal.view.text)

# the configured cap still applies on the primary screen
terminal, stream = make_terminal(rows=5, scrollback=20)
terminal.offset = 40
terminal.view = FakeView("\n".join("row {}".format(i) for i in range(40)))
command = make_command(terminal.view)
command.trim_history(None, terminal)
rows_left = terminal.view.text.count("\n") + 1
assert rows_left <= 20, rows_left
assert "row 39" in terminal.view.text, "newest rows must survive"
assert "row 0" not in terminal.view.text, "oldest rows must be dropped"

# below the cap nothing is dropped
terminal, stream = make_terminal(rows=5, scrollback=5000)
terminal.offset = 3
terminal.view = FakeView("\n".join("row {}".format(i) for i in range(8)))
command = make_command(terminal.view)
command.trim_history(None, terminal)
assert "row 0" in terminal.view.text and "row 7" in terminal.view.text, \
    terminal.view.text

# nor may its blank rows be trimmed: an app that blanks and refills its last
# row (vim's showcmd corner) would resize the view under a pinned viewport
terminal, stream = make_terminal(rows=4)
stream.feed("\x1b[?1049h")
terminal.view = FakeView("top\nrow\n\n")
command = make_command(terminal.view)
before = terminal.view.text
command.trim_trailing_spaces(None, terminal)
assert terminal.view.text == before, \
    "alt screen blank tail was trimmed: {!r}".format(terminal.view.text)

# on the primary screen the blank tail below the cursor still goes
terminal, stream = make_terminal(rows=4)
terminal.view = FakeView("top\nrow\n\n")
command = make_command(terminal.view)
command.trim_trailing_spaces(None, terminal)
assert terminal.view.text == "top\nrow", terminal.view.text

print("render tests passed (alt screen keeps its view, cap honoured)")


# a selection the user made must survive app output: focus_cursor runs on every
# frame, and clearing it there made click-drag selection impossible to hold
terminal, stream = make_terminal(rows=5, cols=20)
terminal.view = FakeView("hello world\nsecond line\n")
stream.feed("x")
selection = terminal.view.sel()
selection.add(sublime.Region(0, 5))            # user selected "hello"
focus_cursor(terminal.view, None, terminal)
assert len(selection) == 1 and selection[0].begin() == 0 and selection[0].end() == 5, \
    "user selection was clobbered by a render"

# an empty selection (a plain click, or none at all) still gets the caret placed
# at the terminal cursor
selection.clear()
focus_cursor(terminal.view, None, terminal)
assert len(selection) == 1 and selection[0].empty(), \
    "caret must be placed when nothing is selected"

print("selection tests passed (user selection survives renders, caret still tracked)")


# an app that hides the cursor still needs the caret: sublime drops a space
# keypress in a view that has no caret at all
selection.clear()
stream.feed("\x1b[?25l")
focus_cursor(terminal.view, None, terminal)
assert len(selection) == 1 and selection[0].empty(), \
    "caret must survive DECTCEM, or the space key stops working"

print("cursor visibility tests passed (hidden cursor keeps the caret)")


# colorize_line positions regions by arithmetic from the line start now, and
# batches the runs that share a scope into one add_regions call
terminal, stream = make_terminal(rows=3, cols=20)
terminal.view = FakeView()
command = make_command(terminal.view)
stream.feed("\x1b[31mRED\x1b[0m..\x1b[31mred\x1b[0m..\x1b[32mgreen")
command.update_lines(None, terminal)

painted = terminal.view.regions
scopes = sorted(scope for scope, _ in painted.values())
assert scopes == ["vterm.green.default", "vterm.red.default"], scopes
red = next(spans for scope, spans in painted.values() if scope.endswith("red.default"))
assert red == [(0, 3), (5, 8)], "two red runs must share one key: {}".format(red)
green = next(spans for scope, spans in painted.values() if scope.endswith("green.default"))
assert green == [(10, 15)], green
assert terminal.view.text.startswith("RED..red..green"), repr(terminal.view.text)

# the default-on-default run between them gets no region at all
assert len(painted) == 2, painted

# keys are unique per view and recorded so decolorize_line can erase them
assert sorted(terminal.view.regions) == sorted(command.colored_lines[0]), \
    "colored_lines must track exactly the keys that were added"
command.decolorize_line(0)
assert terminal.view.regions == {}, "decolorize must erase every key it added"

print("colorize tests passed (arithmetic offsets, scope batching, key bookkeeping)")


# rows that leave the screen have their colours merged onto shared per-(scope,
# chunk) keys once the chunk closes; per-row keys would otherwise accumulate for
# the whole session, and merging every frame would re-send the whole chunk
terminal, stream = make_terminal(rows=3, cols=20)
terminal.view = FakeView()
command = make_command(terminal.view)
for i in range(300):
    stream.feed("\x1b[31mred{}\x1b[0m\r\n".format(i))
    command.update_lines(None, terminal)
    terminal.screen.dirty.clear()

regions = terminal.view.regions
chunk = "vterm@0@vterm.red.default"
assert chunk in regions, sorted(regions)
_, spans = regions[chunk]
assert len(spans) == CHUNK, "the closed chunk merges its rows: {}".format(len(spans))
assert "vterm@1@vterm.red.default" not in regions, "an open chunk must not be sent yet"
row_keys = [k for k in regions if k.startswith("vterm#")]
assert len(row_keys) == 300 - CHUNK, \
    "merged rows drop their per-row keys, the open chunk keeps them: {}".format(len(row_keys))

# trimming settles the open chunk, then erases the keys of chunks above the cut
terminal, stream = make_terminal(rows=3, scrollback=30)
terminal.view = FakeView()
command = make_command(terminal.view)
stream.feed("".join("\x1b[31mr{}\x1b[0m\r\n".format(i) for i in range(300)))
command.update_lines(None, terminal)
terminal.screen.dirty.clear()
assert set(command._archived_chunks) == {0}, command._archived_chunks
command.trim_history(None, terminal)
assert "vterm@0@vterm.red.default" not in terminal.view.regions, "trimmed chunk key left behind"
assert 0 not in command._archived_chunks
assert "vterm@1@vterm.red.default" in terminal.view.regions, "boundary chunk must survive"

print("scrollback archive tests passed (chunk keys bounded, trim erases whole chunks)")


# a fresh terminal must not be left with the prompt selected: our own replace
# expands the caret across the inserted text, and sublime paints that with the
# selection colour (its default is pure blue)
terminal, stream = make_terminal(rows=3, cols=20)
terminal.view = FakeView()
terminal.view.sel().add(sublime.Region(0, 0))       # the caret of a new view
Terminal._terminals[id(terminal)] = terminal
terminal.view.id = lambda: id(terminal)
terminal.default_title = "zsh"
command = make_command(terminal.view)
stream.feed("[vterm] %")
command.run(None)
assert all(r.empty() for r in terminal.view.sel()), \
    "prompt was left selected: {}".format([(r.begin(), r.end()) for r in terminal.view.sel()])

# but a selection the user already had is still respected
terminal.view.sel().clear()
terminal.view.sel().add(sublime.Region(2, 8))
stream.feed("\x1b[2Kmore output")
command.run(None)
kept = [(r.begin(), r.end()) for r in terminal.view.sel()]
assert any(b != e for b, e in kept), "user selection was dropped: {}".format(kept)

# the dirty set is a repaint hint: rows whose content did not change must not
# be rewritten (each rewrite is a cross-process call and an on_modified)
rewritten = []
_replace = terminal.view.replace
terminal.view.replace = lambda edit, region, value: (rewritten.append(value), _replace(edit, region, value))[1]
terminal.screen.dirty.update(range(terminal.screen.lines))
command.run(None)
terminal.view.replace = _replace
# blank tail rows still churn via trim_trailing_spaces; content rows must not
assert all(text == "" for text in rewritten), "unchanged rows were rewritten: {}".format(rewritten)
print("render diff tests passed (unchanged dirty rows skipped)")

# the generated scheme must define a selection colour, or sublime uses blue
from vterm.scheme import blend, scheme_manager
scheme_manager.background = "#303841"
scheme_manager.foreground = "#d8dee9"
assert blend(scheme_manager.background, scheme_manager.foreground, 0.3) == "#626a73"

print("selection artefact tests passed (fresh prompt unselected, user selection kept)")


# DECSCUSR drives the caret shape through view settings; before any app asks,
# and for 0, the caret is the terminal default: a blinking block
terminal, stream = make_terminal(rows=3, cols=20)
command = make_command(terminal.view)
settings = terminal.view.settings()

command.sync_cursor_style(terminal)
assert settings.get("block_caret") is True, settings.get("block_caret")
assert settings.get("caret_style") == "blink", settings.get("caret_style")

stream.feed("\x1b[2 q")
command.sync_cursor_style(terminal)
assert settings.get("block_caret") is True, settings.get("block_caret")
assert settings.get("caret_style") == "solid", settings.get("caret_style")

stream.feed("\x1b[5 q")
command.sync_cursor_style(terminal)
assert settings.get("block_caret") is False, settings.get("block_caret")
assert settings.get("caret_style") == "blink", settings.get("caret_style")

stream.feed("\x1b[0 q")
command.sync_cursor_style(terminal)
assert settings.get("block_caret") is True, settings.get("block_caret")
assert settings.get("caret_style") == "blink", settings.get("caret_style")

# a hidden cursor outranks DECSCUSR: the caret stays for typing, as a block on
# the cell the app draws its own cursor over
stream.feed("\x1b[5 q\x1b[?25l")
command.sync_cursor_style(terminal)
assert settings.get("block_caret") is True, settings.get("block_caret")
assert settings.get("caret_style") == "solid", settings.get("caret_style")

stream.feed("\x1b[?25h")
command.sync_cursor_style(terminal)
assert settings.get("block_caret") is False, settings.get("block_caret")
assert settings.get("caret_style") == "blink", settings.get("caret_style")

print("cursor style tests passed (DECSCUSR maps to caret settings, 0 is the default block)")


# wheel over an alt-screen app that does not track the mouse must fall back to
# arrow keys (alternate scroll); with tracking on, SGR reports as before
terminal, stream = make_terminal(rows=5, cols=20)
Terminal._terminals[id(terminal)] = terminal
terminal.view.id = lambda: id(terminal)
scroll = VtermScrollCommand.__new__(VtermScrollCommand)
scroll.view = terminal.view

stream.feed("\x1b[?1049h")
scroll.run(None, up=True)
assert terminal.process.written == "\x1b[A" * 3, repr(terminal.process.written)

# application cursor mode switches the arrow encoding
terminal.process.written = ""
stream.feed("\x1b[?1h")
scroll.run(None, up=False)
assert terminal.process.written == "\x1bOB" * 3, repr(terminal.process.written)

# with mouse tracking on, the wheel is reported, not translated: one report per
# line, aimed mid-screen when there is no event to place it
terminal.process.written = ""
stream.feed("\x1b[?1002h\x1b[?1006h")
scroll.run(None, up=True)
assert terminal.process.writes == ["\x1b[<64;1;3M"] * 3, terminal.process.writes

# on the primary screen without tracking the command stays inert
terminal.process.written = ""
stream.feed("\x1b[?1002l\x1b[?1006l\x1b[?1049l")
scroll.run(None, up=True)
assert terminal.process.written == "", repr(terminal.process.written)

print("alternate scroll tests passed (3 lines per tick: arrows on alt screen, SGR when tracking, inert otherwise)")


# with the configured modifier PgUp/PgDn page the view and Up/Down move it one
# row, instead of reaching the app; every other combination is sent as before.
# 24 scrollback rows above an 8-row screen, viewport at the follow anchor
terminal, stream = make_terminal(rows=8, cols=20)
Terminal._terminals[id(terminal)] = terminal
terminal.view.id = lambda: id(terminal)
terminal.view.text = "\n".join("row {}".format(i) for i in range(32))
terminal.view.viewport_rows = terminal.screen.lines
terminal.offset = 24
keypress = VtermKeypressCommand.__new__(VtermKeypressCommand)
keypress.view = terminal.view
plugin_settings = sublime.load_settings(SETTINGS_FILE)
PAGE = terminal.screen.lines * LINE_HEIGHT
anchor = 24 * LINE_HEIGHT


def press(**kwargs):
    terminal.process.written = ""
    keypress.run(None, **kwargs)
    return terminal.view.viewport_position()[1]


scroll_to_cursor(terminal.view, terminal)
assert following(terminal.view, terminal)

y = press(key="pageup", shift=True)
assert y == anchor - PAGE, y
assert terminal.process.written == "", repr(terminal.process.written)
# output must not yank the viewport back while the user reads
assert not following(terminal.view, terminal)

# never above the top, never below the anchor
for _ in range(3):
    y = press(key="pageup", shift=True)
assert y == 0, y
for _ in range(5):
    y = press(key="pagedown", shift=True)
assert y == anchor, y
# paging back onto the anchor re-opens the gate
assert following(terminal.view, terminal)

# the arrows move one row rather than a screen
y = press(key="up", shift=True)
assert y == anchor - LINE_HEIGHT, y
assert terminal.process.written == "", repr(terminal.process.written)
# one row up is the edge of the gate's one-line tolerance; it must still close
assert not following(terminal.view, terminal)

for _ in range(30):
    y = press(key="up", shift=True)
assert y == 0, y
for _ in range(30):
    y = press(key="down", shift=True)
assert y == anchor, y
assert following(terminal.view, terminal)

# a bare or differently modified key still belongs to the app
press(key="pageup")
assert terminal.process.written == "\x1b[5~", repr(terminal.process.written)
press(key="pageup", ctrl=True)
assert terminal.process.written == "\x1b[5;5~", repr(terminal.process.written)
y = press(key="pageup", ctrl=True, shift=True)
assert terminal.process.written == "\x1b[5;6~", repr(terminal.process.written)
assert y == anchor, "the app's keys must not scroll"
press(key="up")
assert terminal.process.written == "\x1b[A", repr(terminal.process.written)
y = press(key="up", ctrl=True, shift=True)
assert terminal.process.written == "\x1b[1;6A", repr(terminal.process.written)
assert y == anchor, "the app's keys must not scroll"

# the alt screen is folded away: nothing to scroll, so the app keeps the keys
stream.feed("\x1b[?1049h")
press(key="pageup", shift=True)
assert terminal.process.written == "\x1b[5;2~", repr(terminal.process.written)
press(key="up", shift=True)
assert terminal.process.written == "\x1b[1;2A", repr(terminal.process.written)
stream.feed("\x1b[?1049l")

# the modifier is configurable, and null hands every key back to the app
plugin_settings.set("scroll_modifier", "ctrl")
y = press(key="pageup", ctrl=True)
assert y == anchor - PAGE, y
y = press(key="up", ctrl=True)
assert y == anchor - PAGE - LINE_HEIGHT, y
press(key="pageup", shift=True)
assert terminal.process.written == "\x1b[5;2~", repr(terminal.process.written)
press(key="up", shift=True)
assert terminal.process.written == "\x1b[1;2A", repr(terminal.process.written)

plugin_settings.set("scroll_modifier", None)
press(key="pageup", shift=True)
assert terminal.process.written == "\x1b[5;2~", repr(terminal.process.written)
press(key="up", shift=True)
assert terminal.process.written == "\x1b[1;2A", repr(terminal.process.written)
press(key="pagedown", ctrl=True)
assert terminal.process.written == "\x1b[6;5~", repr(terminal.process.written)
plugin_settings.set("scroll_modifier", "shift")

print("view scroll tests passed (a screen or a row per press, clamped to the anchor, configurable modifier)")


# an app that repaints its block shorter than the screen (pi on every SIGWINCH)
# leaves the live screen shorter than the viewport once the blank tail is
# trimmed. the anchor must stay where sublime can actually put the viewport, or
# the follow gate closes for good and the output is never followed again
short, _ = make_terminal(rows=8, cols=20)
short.view.viewport_rows = short.screen.lines
# 24 scrollback rows, then a live screen holding 3 rows instead of 8
short.view.text = "\n".join("row {}".format(i) for i in range(27))
short.offset = 24
scroll_to_cursor(short.view, short)
assert short.viewport_y == short.view.viewport_position()[1], \
    (short.viewport_y, short.view.viewport_position()[1])
assert following(short.view, short), "follow gate latched shut on a short screen"

print("short screen tests passed (the anchor stays reachable, so the view keeps following)")


# shift+enter through the whole keypress path: ESC CR by default, and the CSI 27
# form once the app turned modifyOtherKeys on, which it reaches for because a
# bare ESC CR reads as alt+enter
press(key="enter", shift=True)
assert terminal.process.written == "\x1b\r", repr(terminal.process.written)
stream.feed("\x1b[>4;2m")
press(key="enter", shift=True)
assert terminal.process.written == "\x1b[27;2;13~", repr(terminal.process.written)
press(key="enter")
assert terminal.process.written == "\r", "unmodified enter still submits"
press(key="c", ctrl=True)
assert terminal.process.written == "\x03", "ctrl+c stays a control byte"
stream.feed("\x1b[>4;0m")
press(key="enter", shift=True)
assert terminal.process.written == "\x1b\r", repr(terminal.process.written)

print("modifyOtherKeys tests passed (shift+enter as CSI 27 while the app asks for it)")


# a click in an unfocused group must focus the tab instead of reporting: we
# replaced drag_select, which is what sublime focuses the group from
class FakeWindow:
    def __init__(self, active):
        self.active = active

    def active_view(self):
        return self.active

    def focus_view(self, view):
        self.active = view


terminal, stream = make_terminal(rows=5, cols=20)
Terminal._terminals[id(terminal)] = terminal
terminal.view.id = lambda: id(terminal)
terminal.view.window_to_text = lambda xy: 0
window = FakeWindow(active=object())
terminal.view.window = lambda: window
stream.feed("\x1b[?1002h\x1b[?1006h")
click = VtermMouseClickCommand.__new__(VtermMouseClickCommand)
click.view = terminal.view
event = {"x": 0, "y": 0, "button": 1}

click.run(None, event)
assert window.active_view() is terminal.view, "click did not focus the tab"
assert terminal.process.written == "", repr(terminal.process.written)

# once focused, the same click reports as before
click.run(None, event)
assert terminal.process.written == "\x1b[<0;1;1M\x1b[<0;1;1m", repr(terminal.process.written)

print("click focus tests passed (first click focuses only, then reports)")


# entering the alt screen folds the scrollback away (kitty hides it); leaving
# unfolds it, and the fold resets the follow anchor so the gate re-engages.
# the fold ends one character before the screen's first row, out of reach of
# the per-frame replaces of that row
terminal, stream = make_terminal(rows=5, cols=20)
terminal.view = FakeView("old1\nold2\nold3\nr0\nr1\nr2\nr3\nr4\n")
terminal.offset = 3
terminal.viewport_y = 400
command = make_command(terminal.view)
fold_end = terminal.view.text_point(3, 0) - 1

stream.feed("\x1b[?1049h")
command.sync_alt_fold(terminal)
assert terminal.view.folds == [(0, fold_end)], terminal.view.folds
assert terminal.viewport_y == 0, terminal.viewport_y

# a second render leaves the fold alone
command.sync_alt_fold(terminal)
assert terminal.view.folds == [(0, fold_end)], terminal.view.folds

# a disturbed fold is re-asserted on the next render, keeping the anchor:
# apps repaint the boundary row every scroll, and a per-repair reset dropped
# the user's scrollback position on every frame
terminal.viewport_y = 60
terminal.view.folds = [(0, fold_end - 2)]
command.sync_alt_fold(terminal)
assert terminal.view.folds == [(0, fold_end)], terminal.view.folds
assert terminal.viewport_y == 60, terminal.viewport_y

stream.feed("\x1b[?1049l")
command.sync_alt_fold(terminal)
assert terminal.view.folds == [], terminal.view.folds

# without scrollback there is nothing to fold
terminal, stream = make_terminal(rows=5, cols=20)
terminal.view = FakeView("r0\nr1\nr2\nr3\nr4\n")
command = make_command(terminal.view)
stream.feed("\x1b[?1049h")
command.sync_alt_fold(terminal)
assert terminal.view.folds == [], terminal.view.folds

print("alt fold tests passed (scrollback folded on entry, restored on exit)")



# a row whose first cells were never written: they read back as default cells
# (StaticDefaultDict), so they form their own uncoloured run and the coloured
# run that follows keeps the right column
from pyte.screens import StaticDefaultDict
from vterm.emulator import Char
from vterm.render import segment_buffer_line

line = StaticDefaultDict(Char(" "))
line[3] = Char("X", "red", "default")
runs = [r for r in segment_buffer_line(line) if r[1] != r[2]]
assert runs == [("   ", 0, 3, "default", "default", ""),
                ("X", 3, 4, "red", "default", "")], runs

# wide characters consume two columns but one character of text
line = StaticDefaultDict(Char(" "))
line[0] = Char("漢", "red", "default")
line[1] = Char("", "red", "default")
line[2] = Char("x", "blue", "default")
runs = [r for r in segment_buffer_line(line) if r[1] != r[2]]
assert runs == [("漢", 0, 1, "red", "default", ""), ("x", 1, 2, "blue", "default", "")], runs

# an orphaned wide-char filler (its wide half was overwritten with a narrow
# char) still occupies a column: rendered as a space so later spans stay aligned
line = StaticDefaultDict(Char(" "))
line[0] = Char("a", "default", "default")
line[1] = Char("", "red", "default")
line[2] = Char("x", "blue", "default")
runs = [r for r in segment_buffer_line(line) if r[1] != r[2]]
assert runs == [("a", 0, 1, "default", "default", ""),
                (" ", 1, 2, "red", "default", ""),
                ("x", 2, 3, "blue", "default", "")], runs

# reverse video swaps the pair, and default becomes reverse_default
line = StaticDefaultDict(Char(" "))
line[0] = Char("r", "red", "default", False, False, False, False, True)
runs = [r for r in segment_buffer_line(line) if r[1] != r[2]]
assert runs == [("r", 0, 1, "reverse_default", "red", "")], runs

print("segmenter tests passed (leading gap, wide char, orphan filler, reverse)")
