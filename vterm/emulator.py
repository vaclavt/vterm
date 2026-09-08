""" Terminal emulation: pyte screen and a parser that handles prefixed CSI, DECRQM and multi-digit OSC """
import base64
import logging
import re
import time
import unicodedata
from collections import defaultdict, deque, namedtuple

import pyte
from pyte import control as ctrl
from pyte import graphics as g
from pyte import modes as mo
from pyte.charsets import LAT1_MAP
from pyte.screens import Margins, StaticDefaultDict, wcwidth
from wcwidth import wcswidth

LOG = logging.getLogger(__name__)

# pyte's colour names, in SGR 30-37 / 90-97 order; the index is the 256-palette entry
ANSI_COLORS = [
    "black",
    "red",
    "green",
    "brown",
    "blue",
    "magenta",
    "cyan",
    "white",
    "light_black",
    "light_red",
    "light_green",
    "light_brown",
    "light_blue",
    "light_magenta",
    "light_cyan",
    "light_white",
]

# SGR 90-97 / 100-107: the bright colours, which stock pyte does not map
FG_AIXTERM = {90 + i: ANSI_COLORS[8 + i] for i in range(8)}
BG_AIXTERM = {100 + i: ANSI_COLORS[8 + i] for i in range(8)}

# DECRQM answers, as wire numbers (pyte stores modes shifted << 5)
REPORTABLE_MODES = (mo.DECAWM >> 5, mo.DECTCEM >> 5, 1000, 1002, 1003, 1004, 1006, 1049, 2004, 2026)

# DEC private modes, pre-shifted into pyte's internal representation.
# 1: DECCKM, application cursor keys (arrows send SS3); pyte has no name for it
APP_CURSOR_MODE = 1 << 5
# 2026: synchronized output, the app is mid-frame and we should not draw
SYNC_MODE = 2026 << 5
# 1049: alternate screen, a full-screen app owns the viewport
ALT_SCREEN_MODE = 1049 << 5
# 2004: wrap pasted text in ESC[200~ / ESC[201~ so the app can tell it from typing
BRACKETED_PASTE_MODE = 2004 << 5
# 1004: report focus gain/loss as ESC[I / ESC[O
FOCUS_MODE = 1004 << 5
# 1000 click only, 1002 click plus drag, 1003 any motion
MOUSE_MODES = (1000 << 5, 1002 << 5, 1003 << 5)
# 1006: SGR mouse encoding, the only wire form we emit
MOUSE_SGR_MODE = 1006 << 5
# 2031: the app wants to be told when the colour scheme changes
THEME_NOTIFY_MODE = 2031 << 5

# how long a ?2026 frame may suppress rendering before we draw anyway
SYNC_FRAME_TIMEOUT = 0.15

# must outlast Terminal.REPAINT_NUDGE_DELAY, which triggers the repaint
SUPPRESS_HISTORY_AFTER_CLEAR = 0.8


def _copy_line(line):
    """copy.copy on a dict subclass takes the pickle path (~10x); rebuild instead"""
    new = StaticDefaultDict(line.default)
    dict.update(new, line)
    return new


def quantize_256(r, g, b):
    """Nearest xterm-256 index: grays to the ramp, colours to the 6x6x6 cube.

    Truecolor is folded into the 256 palette so the colour-scheme rule table
    stays bounded (each distinct combo becomes a scheme rule and a file write)."""
    if r == g == b:
        if r < 8:
            return 16
        if r > 248:
            return 231
        return 232 + (r - 8) // 10

    def level(c):
        return 0 if c < 48 else (1 if c < 115 else (c - 35) // 40)

    return 16 + 36 * level(r) + 6 * level(g) + level(b)


# linefeed marks an autowrap (soft wrap), so copy can re-join the halves
Char = namedtuple(
    "Char",
    ["data", "fg", "bg", "bold", "italics", "underscore", "strikethrough", "reverse", "linefeed"],
    defaults=("default", "default", False, False, False, False, False, False),
)


class Cursor:
    """Cursor position and pending attributes.

    Replaces pyte's own so a Char carrying the extra `linefeed` field can be
    the default attribute set.
    """

    __slots__ = ("x", "y", "attrs", "hidden")

    def __init__(self, x, y, attrs=Char(" ")):
        """Args:
        attrs: the Char whose styling new text inherits.
        """
        self.x = x
        self.y = y
        self.attrs = attrs
        self.hidden = False


class VtScreen(pyte.Screen):
    """Screen with the terminal features these agents rely on.

    Adds to pyte: the alternate screen, scrollback, synchronized output,
    mouse/focus/paste modes, cursor styles, query replies and OSC handling.
    Mutated only by the renderer thread, so it carries no lock.
    """

    @property
    def default_char(self):
        """The blank cell an untouched column holds."""
        # pyte's base property cannot be shadowed by an instance attribute;
        # the value is maintained on reset and DECSCNM flips
        return self._default_char

    def __init__(
        self,
        columns,
        lines,
        process,
        history=5000,
        erase_scrollback=True,
        clear_callback=lambda: None,
        clipboard_callback=lambda text: None,
        fg_color="ffffff",
        bg_color="000000",
        is_dark=True,
    ):
        """Args:
        process: written to directly when the app queries the terminal.
        history: scrollback depth, in lines.
        erase_scrollback: whether ED 3 drops the scrollback, as a terminal should.
        clear_callback: the app cleared the screen; the view follows.
        clipboard_callback: an OSC 52 payload for the system clipboard.
        fg_color, bg_color, is_dark: palette facts reported back to the app.
        """
        self._process = process
        self._erase_scrollback = erase_scrollback
        self._clear_callback = clear_callback
        self._clipboard_callback = clipboard_callback
        self.primary_buffer = {}
        self._default_char = Char(data=" ", fg="default", bg="default", reverse=False)
        # SGR transitions repeat endlessly in TUI output; memoized per (attrs, params)
        self._sgr_cache = {}
        # chars are identical while cursor.attrs is unchanged (draw rebuilds per run)
        self._char_cache = {}
        self._cache_attrs = None
        self.history = deque(maxlen=history)
        self.alternate_buffer_mode = False
        self._sync_frame_start = 0
        self._suppress_history_until = 0
        # rgb hex (no '#'), answered to OSC 10/11 colour queries
        self.default_fg_color = fg_color
        self.default_bg_color = bg_color
        self.theme_is_dark = is_dark
        super().__init__(columns, lines)

    def reset(self):
        """RIS: pyte's reset plus our cursor, scrollback and caches."""
        self._default_char = Char(data=" ", fg="default", bg="default", reverse=False)
        self._sgr_cache.clear()
        super().reset()
        self.cursor = Cursor(0, 0)
        self.cursor_style = 0
        self.history.clear()
        self._clear_callback()

    def set_cursor_style(self, style):
        """Record the caret shape the app asked for; render maps it onto sublime."""
        # DECSCUSR: 0 default, 1/2 block, 3/4 underline, 5/6 bar; odd = blinking
        self.cursor_style = style

    def resize(self, lines=None, columns=None):
        """Resize the screen, pushing scrolled-off lines into scrollback.

        Unlike pyte's, shrinking preserves content: rows that no longer fit go
        to history rather than being dropped.
        """
        lines = lines or self.lines
        columns = columns or self.columns

        if lines == self.lines and columns == self.columns:
            return

        self.dirty.update(range(lines))

        # shrinking: anything below the last non-empty row is free to lose,
        # the rest scrolls into history
        line_diff = self.lines - lines
        if line_diff > 0:
            bottom = self.first_non_empty_line_from_bottom()
            num_empty_lines = self.lines - 1 - bottom
            if line_diff > num_empty_lines:
                line_diff = line_diff - num_empty_lines
                self.push_lines_into_history(line_diff)
                self.scroll_up(line_diff)
                self.cursor.y -= line_diff

        if columns < self.columns:
            # leftovers past the new width would keep rendering
            buffers = [self.buffer]
            if self.primary_buffer.get("buffer") is not None:
                buffers.append(self.primary_buffer["buffer"])
            for buffer in buffers:
                for line in buffer.values():
                    for x in range(columns, self.columns):
                        line.pop(x, None)

        self.lines, self.columns = lines, columns
        # pyte clamps via restore_cursor; without it a shrink leaves the cursor
        # negative or past the new edge. x == columns keeps a pending wrap
        self.cursor.x = min(self.cursor.x, columns)
        self.cursor.y = min(max(self.cursor.y, 0), lines - 1)
        self.set_margins()
        self.tabstops = set(range(8, self.columns, 8))

    def set_mode(self, *modes, **kwargs):
        """Track DECSET, entering the alternate screen and opening ?2026 frames."""
        super().set_mode(*modes, **kwargs)
        self._sync_default_char()
        if kwargs.get("private") and 2026 in modes:
            self._sync_frame_start = time.time()
        if ALT_SCREEN_MODE in self.mode and not self.alternate_buffer_mode:
            self.alternate_buffer_mode = True
            self.save_cursor()
            self.switch_to_screen(alt=True)

    def reset_mode(self, *modes, **kwargs):
        """Track DECRST, leaving the alternate screen and restoring the cursor."""
        super().reset_mode(*modes, **kwargs)
        self._sync_default_char()
        if ALT_SCREEN_MODE not in self.mode and self.alternate_buffer_mode:
            self.alternate_buffer_mode = False
            self.switch_to_screen(alt=False)
            self.restore_cursor()

    def _sync_default_char(self):
        """Refresh the blank cell after a DECSCNM (reverse video) flip."""
        reverse = mo.DECSCNM in self.mode
        if reverse != self._default_char.reverse:
            self._default_char = self._default_char._replace(reverse=reverse)
            self._sgr_cache.clear()
            # pyte points every line at the old blank cell from inside
            # set/reset_mode, before this runs; a stale one paints the columns
            # the app never wrote reverse for good
            for line in self.buffer.values():
                line.default = self._default_char

    def switch_to_screen(self, alt=False):
        """Swap buffer, scrollback and cursor between the primary and alternate screens.

        The alternate screen gets a maxlen=0 history: a full-screen app must not
        push its frames into scrollback.
        """
        if alt:
            self.primary_buffer["buffer"] = self.buffer
            self.primary_buffer["history"] = self.history
            self.primary_buffer["cursor"] = self.cursor
            self.buffer = defaultdict(lambda: StaticDefaultDict(self.default_char))
            self.history = deque(maxlen=0)
            self.cursor = Cursor(0, 0)
        else:
            self.buffer = self.primary_buffer["buffer"]
            self.history = self.primary_buffer["history"]
            self.cursor = self.primary_buffer["cursor"]

        self.dirty.update(range(self.lines))

    def wipe(self):
        """Blank the screen and home the cursor"""
        for y in range(self.lines):
            self.buffer[y].clear()
        self.cursor.x = 0
        self.cursor.y = 0
        self.dirty.update(range(self.lines))
        self._suppress_history_until = time.time() + SUPPRESS_HISTORY_AFTER_CLEAR

    def in_sync_frame(self):
        """True while an app is mid ?2026 frame, so rendering should wait.

        Bounded by SYNC_FRAME_TIMEOUT: an app that never closes its frame
        must not freeze the view.
        """
        return SYNC_MODE in self.mode and time.time() - self._sync_frame_start <= SYNC_FRAME_TIMEOUT

    def mouse_reporting_enabled(self):
        """True when the app asked for mouse reports we can actually encode."""
        return MOUSE_SGR_MODE in self.mode and any(m in self.mode for m in MOUSE_MODES)

    def cursor_hidden(self):
        """True while the app has DECTCEM off (CSI ?25l).

        Read from the mode set, not cursor.hidden: switch_to_screen builds a
        fresh Cursor, which would flash the caret on every alt-screen entry.
        """
        return mo.DECTCEM not in self.mode

    def theme_notify_enabled(self):
        """True when the app subscribed to colour scheme changes (?2031)."""
        return THEME_NOTIFY_MODE in self.mode

    def focus_tracking_enabled(self):
        """True when the app wants focus reports (?1004)."""
        return FOCUS_MODE in self.mode

    def bracketed_paste_enabled(self):
        """True when pastes should be bracketed (?2004)."""
        return BRACKETED_PASTE_MODE in self.mode

    def draw(self, data):
        """Write text at the cursor, wcwidth-aware, marking hard wraps.

        Differs from pyte: double-width cells claim two columns, combining marks
        are merged into the preceding cell as NFC (sublime renders decomposed
        sequences badly), and an autowrap sets `linefeed` on the last cell so
        copy can tell a soft wrap from a real newline.
        """
        if self.charset:
            data = data.translate(self.g1_charset)
        elif self.g0_charset is not LAT1_MAP:
            data = data.translate(self.g0_charset)

        autowrap = mo.DECAWM in self.mode
        insert_mode = mo.IRM in self.mode

        # hoisted: this loop runs per printable character of every burst
        cursor = self.cursor
        buffer = self.buffer
        columns = self.columns
        # cells only differ by their data while attrs are unchanged, so a
        # per-run cache turns the namedtuple rebuild into a dict lookup
        if cursor.attrs is not self._cache_attrs:
            self._cache_attrs = cursor.attrs
            self._char_cache = {}
        cache = self._char_cache
        line = buffer[cursor.y]

        for char in data:
            char_width = wcwidth(char)
            if (cursor.x == columns and char_width >= 1) or (cursor.x == columns - 1 and char_width >= 2):
                if autowrap:
                    last = line[columns - 1]
                    line[columns - 1] = last._replace(linefeed=True)
                    self.dirty.add(cursor.y)
                    self.carriage_return()
                    self.linefeed()
                    line = buffer[cursor.y]
                elif char_width > 0:
                    cursor.x -= char_width

            if char_width == 1 or char_width == 2:
                if insert_mode:
                    self.insert_characters(char_width)
                cell = cache.get(char)
                if cell is None:
                    cell = cache[char] = cursor.attrs._replace(data=char)
                line[cursor.x] = cell
                if char_width == 2 and cursor.x + 1 < columns:
                    filler = cache.get("")
                    if filler is None:
                        filler = cache[""] = cursor.attrs._replace(data="")
                    line[cursor.x + 1] = filler
            # a combining mark belongs to the cell before it, which may be the
            # last column of the previous row
            elif char_width == 0 and unicodedata.combining(char):
                # sublime renders decomposed combining chars poorly; merge as NFC
                pos = None
                last = None
                for row, col in [(self.cursor.y, self.cursor.x), (self.cursor.y - 1, self.columns)]:
                    if row < 0:
                        continue
                    row_line = self.buffer[row]
                    if col >= 2:
                        last = row_line[col - 2]
                        if wcswidth(last.data) >= 2:
                            pos = (row, col - 2)
                            break
                    if col >= 1:
                        last = row_line[col - 1]
                        pos = (row, col - 1)
                        break

                if pos and last is not None:
                    normalized = unicodedata.normalize("NFC", last.data + char)
                    self.buffer[pos[0]][pos[1]] = last._replace(data=normalized)
                    self.dirty.add(pos[0])
            else:
                break

            if char_width > 0:
                self.cursor.x = min(self.cursor.x + char_width, self.columns)

        self.dirty.add(self.cursor.y)

    def index(self):
        """Line feed; at the bottom of the primary screen the top row goes to scrollback."""
        if not self.alternate_buffer_mode and self.cursor.y == self.lines - 1 and self.margins is None:
            self.push_lines_into_history(1)
        super().index()

    def erase_in_display(self, how=0, *args, **kwargs):
        """ED: erase part of the screen, keeping what scrolls away.

        Unlike pyte's, a full clear (or a clear-to-end from home) pushes the
        current screen into scrollback first, so `clear` does not lose history.
        """
        if not self.alternate_buffer_mode and (how == 2 or (how == 0 and self.cursor.x == 0 and self.cursor.y == 0)):
            self.push_lines_into_history()

        if how == 3:
            # xterm's ED 3 erases the scrollback only; the screen stays
            if self._erase_scrollback:
                self.history.clear()
                self._clear_callback()
            return

        if how == 0:
            interval = range(self.cursor.y + 1, self.lines)
        elif how == 1:
            interval = range(self.cursor.y)
        elif how == 2:
            interval = range(self.lines)
        else:
            return

        self.dirty.update(interval)
        for y in interval:
            line = self.buffer[y]
            for x in list(line):
                if x < self.columns:
                    line[x] = self.cursor.attrs
                else:
                    line.pop(x, None)

        if how == 0 or how == 1:
            self.erase_in_line(how)

    def select_graphic_rendition(self, *attrs, **kwargs):
        """Apply an SGR sequence to the cursor attributes.

        Adds to pyte: bright (aixterm) colours, and truecolor folded into the
        256 palette. Transitions are memoized — TUIs cycle through a handful of
        sequences thousands of times per frame.
        """
        if not attrs or attrs == (0,):
            self.cursor.attrs = self._default_char
            return

        cache_key = (self.cursor.attrs, attrs)
        cached = self._sgr_cache.get(cache_key)
        if cached is not None:
            self.cursor.attrs = cached
            return

        # index walk rather than pop(): 38/48 consume their own parameters
        replace = {}
        i = 0
        end = len(attrs)
        while i < end:
            attr = attrs[i]
            i += 1
            if attr == 0:
                replace.update(self._default_char._asdict())
            elif attr in g.FG_ANSI:
                replace["fg"] = g.FG_ANSI[attr]
            elif attr in g.BG_ANSI:
                replace["bg"] = g.BG_ANSI[attr]
            elif attr in g.TEXT:
                attr = g.TEXT[attr]
                if attr[1:] != "blink":
                    replace[attr[1:]] = attr.startswith("+")
            elif attr in FG_AIXTERM:
                replace["fg"] = FG_AIXTERM[attr]
            elif attr in BG_AIXTERM:
                replace["bg"] = BG_AIXTERM[attr]
            elif attr in (g.FG_256, g.BG_256):
                key = "fg" if attr == g.FG_256 else "bg"
                try:
                    n = attrs[i]
                    i += 1
                    if n == 5:
                        m = attrs[i]
                        i += 1
                        if m < 16:
                            replace[key] = ANSI_COLORS[m]
                        else:
                            replace[key] = g.FG_BG_256[m]
                    elif n == 2:
                        m = quantize_256(attrs[i], attrs[i + 1], attrs[i + 2])
                        i += 3
                        replace[key] = ANSI_COLORS[m] if m < 16 else g.FG_BG_256[m]
                except IndexError:
                    break

        result = self.cursor.attrs._replace(**replace)
        if len(self._sgr_cache) > 1024:
            self._sgr_cache.clear()
        self._sgr_cache[cache_key] = result
        self.cursor.attrs = result

    def scroll_up(self, n=None):
        """CSI S: scroll the margin region up, blanking the rows it exposes."""
        n = n or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)
        for y in range(top, bottom + 1):
            if y + n > bottom:
                self.buffer[y].clear()
            else:
                self.buffer[y] = _copy_line(self.buffer[y + n])
        self.dirty.update(range(self.lines))

    def scroll_down(self, n=None):
        """CSI T: scroll the margin region down, blanking the rows it exposes."""
        n = n or 1
        top, bottom = self.margins or Margins(0, self.lines - 1)
        for y in reversed(range(top, bottom + 1)):
            if y - n < top:
                self.buffer[y].clear()
            else:
                self.buffer[y] = _copy_line(self.buffer[y - n])
        self.dirty.update(range(self.lines))

    def first_non_empty_line_from_bottom(self):
        """Index of the lowest row holding text, or -1 when the screen is blank."""
        for nz_line in reversed(range(self.lines)):
            # .get: indexing the defaultdict would materialize every empty row
            line = self.buffer.get(nz_line)
            if line and any(c.data and not c.data.isspace() for c in line.values()):
                return nz_line
        return -1

    def push_lines_into_history(self, count=None):
        """Copy the top `count` rows into scrollback; all content rows by default.

        Suppressed on the alternate screen, and briefly after a wipe so the
        forced repaint does not land in scrollback twice.
        """
        if self.alternate_buffer_mode:
            return
        if time.time() < self._suppress_history_until:
            return
        if count is None:
            count = self.first_non_empty_line_from_bottom() + 1
        self.history.extend(_copy_line(self.buffer[y]) for y in range(count))

    # replies go to the pty, never to the screen

    def write_process_input(self, data):
        """Send a reply to the app. Query answers go to the pty, never to the screen."""
        self._process.write(data)

    def report_device_attributes(self, mode=0, **kwargs):
        """DA1: claim to be a VT220 with ANSI colour. Claude Code waits on this with no timeout."""
        if kwargs.get("private"):
            return
        if mode == 0:
            self.write_process_input("\x1b[?62;22c")

    def report_secondary_device_attributes(self, mode=0):
        """DA2: terminal type and version, as a VT220 would answer."""
        self.write_process_input("\x1b[>1;10;0c")

    def report_device_status(self, mode=0, **kwargs):
        """DSR: 5 device ok, 6 cursor position, private 996 the light/dark scheme."""
        private = kwargs.get("private", False)
        if mode == 5:
            self.write_process_input("\x1b[0n")
        elif mode == 6:
            x = min(self.cursor.x, self.columns - 1) + 1
            y = self.cursor.y + 1
            marker = "?" if private else ""
            self.write_process_input("\x1b[{}{};{}R".format(marker, y, x))
        elif mode == 996 and private:
            # color-scheme report, pi asks with a 2s timeout
            self.write_process_input(self.theme_report())

    def theme_report(self):
        """DSR 997 report: the palette is dark (1) or light (2). Also sent on a live theme change."""
        return "\x1b[?997;{}n".format(1 if self.theme_is_dark else 2)

    def report_xtversion(self):
        """XTVERSION: naming ourselves is what unlocks ?2026 for apps on a generic TERM."""
        self.write_process_input("\x1bP>|vterm 0.1\x1b\\")

    def report_mode_status(self, mode, private):
        """DECRQM: answer 1 set, 2 reset, 0 for modes we do not implement."""
        if not private:
            self.write_process_input("\x1b[{};0$y".format(mode))
            return
        if mode in REPORTABLE_MODES:
            value = 1 if (mode << 5) in self.mode else 2
        else:
            value = 0
        self.write_process_input("\x1b[?{};{}$y".format(mode, value))

    def report_kitty_keyboard(self):
        """Answer the kitty keyboard protocol probe."""
        # 0 = unsupported, so apps fall back to legacy key encodings
        self.write_process_input("\x1b[?0u")

    def handle_osc(self, code, payload, terminator):
        """Dispatch an OSC: 0/2 title, 1 icon, 10/11 colour query, 52 clipboard."""
        if code in (0, 2):
            self.set_title(payload)
        elif code == 1:
            self.set_icon_name(payload)
        elif code in (10, 11):
            if payload == "?":
                color = self.default_fg_color if code == 10 else self.default_bg_color
                rgb = "rgb:{0}{0}/{1}{1}/{2}{2}".format(color[0:2], color[2:4], color[4:6])
                # answer with the terminator asked with: meli only ends an OSC at ST
                self.write_process_input("\x1b]{};{}{}".format(code, rgb, terminator))
        elif code == 52:
            parts = payload.split(";", 1)
            if len(parts) == 2 and parts[1] and parts[1] != "?":
                try:
                    self._clipboard_callback(base64.b64decode(parts[1]).decode("utf-8", "replace"))
                except ValueError:
                    LOG.warning("osc52: undecodable payload", exc_info=True)
        # other OSC codes are intentionally dropped


# a complete plain CSI: params then a final byte. No private marker, prefix or
# intermediate — those forms, and sequences split across reads, take the parser
CSI_COMPLETE = re.compile("(?:\x1b\\[|\x9b)([0-9;]*)([@-~])")
# a read ending mid-CSI; held for the next read, as the parser would hold state.
# bounded so a run of digits with no final cannot buffer forever
CSI_PARTIAL = re.compile("(?:\x1b\\[?|\x9b)[0-9;]{0,32}$")


class VtStream(pyte.Stream):
    """pyte.Stream with a parser for prefixed CSI, DECRQM and multi-digit OSC

    Stock pyte drops what these agents rely on: `>`/`<`/`=` prefixed CSI, the
    DECRQM `$p` intermediate, and OSC codes longer than one digit.

    feed() dispatches complete plain CSI and lone control characters directly:
    pyte resumes the parser coroutine once per character, which was ~90% of the
    cost of an escape-heavy flood.
    """

    def __init__(self, *args, **kwargs):
        """Copy the CSI table before adding to it, so pyte's class-level one is untouched."""
        self.csi = dict(self.csi)
        self.csi["S"] = "scroll_up"
        self.csi["T"] = "scroll_down"
        self._carry = ""
        # param-string -> parsed tuple; SGR strings repeat across a whole frame
        self._params_cache = {}
        super().__init__(*args, **kwargs)

    def attach(self, screen):
        """Bind the fast dispatch tables when the listener is known."""
        super().attach(screen)
        # same construction as the parser's create_dispatcher, so an unknown
        # final lands on listener.debug either way
        self._fast_csi = defaultdict(
            lambda: screen.debug,
            {final: getattr(screen, name) for final, name in self.csi.items()},
        )
        # SI/SO always take the parser: it skips them under utf8, and utf8 can
        # be toggled at runtime
        self._fast_basic = {char: getattr(screen, name) for char, name in self.basic.items() if char not in (ctrl.SI, ctrl.SO)}

    def feed(self, data):
        """pyte's feed with a ground-state fast path.

        Plain text runs keep pyte's regex path. A complete unprefixed CSI is
        matched whole and dispatched in one call, and basic controls (CR, LF,
        tab...) in one lookup, instead of one coroutine resume per character.
        Everything else — OSC, charsets, private/prefixed CSI, `:` subparams —
        falls through to the parser, byte for byte as before. A read ending
        mid-CSI is carried into the next read; the parser would hold it as
        state, so nothing is dispatched either way until the sequence is whole.
        """
        # the tail of the previous read ended mid-CSI; complete it first
        if self._carry:
            data = self._carry + data
            self._carry = ""

        # locals for the loop, which runs per text run / escape sequence
        send = self._send_to_parser
        draw = self.listener.draw
        match_text = self._text_pattern.match
        match_csi = CSI_COMPLETE.match
        match_partial = CSI_PARTIAL.match
        csi = self._fast_csi
        basic = self._fast_basic
        cache = self._params_cache
        # True: the parser is at ground state, the fast paths may act.
        # False: it is mid-sequence and owns every byte until it yields True
        taking_plain_text = self._taking_plain_text

        length = len(data)
        offset = 0
        while offset < length:
            if taking_plain_text:
                # printable run: hand it to draw() whole (pyte's own fast path)
                match = match_text(data, offset)
                if match:
                    start, offset = match.span()
                    draw(data[start:offset])
                    continue
                char = data[offset]
                if char == "\x1b" or char == "\x9b":
                    # complete plain CSI: dispatch here, skipping the parser
                    match = match_csi(data, offset)
                    if match:
                        # params memo: SGR strings repeat across a whole frame
                        raw = match.group(1)
                        params = cache.get(raw)
                        if params is None:
                            params = tuple(min(int(p), 9999) if p else 0 for p in raw.split(";"))
                            if len(cache) > 4096:
                                cache.clear()
                            cache[raw] = params
                        try:
                            csi[match.group(2)](*params)
                        except TypeError:
                            # same guard as _dispatch_csi: excess params cost one sequence, not the chunk
                            LOG.debug("malformed CSI %s%s", params, match.group(2))
                        offset = match.end()
                        continue
                    # CSI cut off by the end of the read: hold it for the next
                    if match_partial(data, offset):
                        self._carry = data[offset:]
                        break
                    # some other escape (OSC, charset, private CSI...):
                    # fall through to the parser
                elif char in basic:
                    # lone control character (CR, LF, tab, BEL...)
                    basic[char]()
                    offset += 1
                    continue
                # this byte is the parser's; the else branch below sends it
                taking_plain_text = False
            else:
                # parser mid-sequence: one byte at a time until it returns
                # True, saying it is back at ground state
                taking_plain_text = send(data[offset : offset + 1])
                offset += 1

        self._taking_plain_text = taking_plain_text

    def _parser_fsm(self):
        """Coroutine pyte feeds one character at a time; `yield True` resumes bulk text.

        A full copy of pyte's FSM rather than a hook, because the additions are
        inside its inner loops. Locals are bound up front: this runs per byte of
        every escape sequence.
        """
        basic = self.basic
        listener = self.listener
        draw = listener.draw
        debug = listener.debug

        ESC, CSI_C1 = ctrl.ESC, ctrl.CSI_C1
        OSC_C1 = ctrl.OSC_C1
        NUL_OR_DEL = ctrl.NUL + ctrl.DEL
        CAN_OR_SUB = ctrl.CAN + ctrl.SUB
        ALLOWED_IN_CSI = "".join([ctrl.BEL, ctrl.BS, ctrl.HT, ctrl.LF, ctrl.VT, ctrl.FF, ctrl.CR])
        OSC_TERMINATORS = set([ctrl.ST_C0, ctrl.ST_C1, ctrl.BEL])
        MAX_OSC = 65536

        # unknown sequences fall through to listener.debug rather than raising
        def create_dispatcher(mapping):
            return defaultdict(lambda: debug, dict((event, getattr(listener, attr)) for event, attr in mapping.items()))

        basic_dispatch = create_dispatcher(basic)
        sharp_dispatch = create_dispatcher(self.sharp)
        escape_dispatch = create_dispatcher(self.escape)
        csi_dispatch = create_dispatcher(self.csi)

        while True:
            char = yield True

            # ESC introducer: fold the 7-bit forms onto their C1 equivalents
            if char == ESC:
                char = yield None
                if char == "[":
                    char = CSI_C1
                elif char == "]":
                    char = OSC_C1
                elif char in "PX^_":
                    # consume the payload so it never reaches the screen
                    prev = ""
                    while True:
                        c = yield None
                        if c == ctrl.ST_C1 or (prev == ESC and c == "\\"):
                            break
                        prev = c
                    continue
                else:
                    if char == "#":
                        sharp_dispatch[(yield None)]()
                    elif char == "%":
                        self.select_other_charset((yield None))
                    elif char in "()":
                        # honoured even in UTF-8 mode, which pyte skips: smacs is ESC(0
                        listener.define_charset((yield None), mode=char)
                    else:
                        escape_dispatch[char]()
                    continue

            if char in basic:
                if (char == ctrl.SI or char == ctrl.SO) and self.use_utf8:
                    continue
                basic_dispatch[char]()
            # CSI: collect private/prefix/intermediate markers and the numeric
            # parameters until a final byte arrives
            elif char == CSI_C1:
                params = []
                current = ""
                private = False
                prefix = ""
                intermediate = ""
                while True:
                    char = yield None
                    if char == "?":
                        private = True
                    elif char in "><=":
                        prefix = char
                    elif char in " $":
                        intermediate = char
                    elif char in ALLOWED_IN_CSI:
                        basic_dispatch[char]()
                    elif char in CAN_OR_SUB:
                        draw(char)
                        break
                    elif char.isdigit():
                        current += char
                    elif char == ":":
                        # SGR subparameters, which we do not support
                        pass
                    elif char == ";":
                        params.append(min(int(current or 0), 9999))
                        current = ""
                    else:
                        params.append(min(int(current or 0), 9999))
                        self._dispatch_csi(csi_dispatch, listener, char, params, private, prefix, intermediate)
                        break
            # OSC: a code of up to 8 digits, then a payload up to MAX_OSC,
            # ending at BEL or ST. pyte only accepts a single-digit code
            elif char == OSC_C1:
                code = ""
                while True:
                    char = yield None
                    if char.isdigit() and len(code) < 8:
                        code += char
                    else:
                        break

                payload = ""
                if char == ";" or (char not in OSC_TERMINATORS and char != ESC):
                    while True:
                        char = yield None
                        if char == ESC:
                            char += yield None
                        if char in OSC_TERMINATORS:
                            break
                        if len(payload) < MAX_OSC:
                            payload += char
                elif char == ESC:
                    # take the '\' of ST, so char holds the whole terminator
                    char += yield None

                try:
                    listener.handle_osc(int(code or 0), payload, char)
                except Exception:
                    LOG.warning("osc handler failed: code=%s", code, exc_info=True)
            elif char not in NUL_OR_DEL:
                draw(char)

    def _dispatch_csi(self, csi_dispatch, listener, final, params, private, prefix, intermediate):
        """Route a CSI by its intermediate, prefix and private marker.

        The forms pyte has no table for are handled here; a private sequence we
        do not implement is dropped rather than run in its non-private meaning.
        """
        if intermediate == "$":
            if final == "p":
                listener.report_mode_status(params[0] if params else 0, private)
            return
        if intermediate == " ":
            if final == "q":
                listener.set_cursor_style(params[0] if params else 0)
            return
        if prefix == ">":
            if final == "q":
                listener.report_xtversion()
            elif final == "c":
                listener.report_secondary_device_attributes(*params)
            # '>m' modifyOtherKeys, '>u' kitty push: ignore
            return
        if prefix:
            # kitty pop ('<u'), DECKPAM-style ('='): ignore
            return
        if private:
            if final == "u":
                listener.report_kitty_keyboard()
            elif final in ("h", "l"):
                csi_dispatch[final](*params, private=True)
            elif final in ("J", "K", "n", "c"):
                csi_dispatch[final](params[0] if params else 0, private=True)
            else:
                # only these handlers understand the private form; passing it to
                # the rest raises TypeError. aerc probes XTSMGRAPHICS (?S)
                LOG.debug("ignoring private CSI ?%s%s", params, final)
            return
        try:
            csi_dispatch[final](*params)
        except TypeError:
            LOG.debug("malformed CSI %s%s", params, final)
