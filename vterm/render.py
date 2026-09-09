""" Rendering the emulator screen into the Sublime view

The view holds real text: scrollback above `terminal.offset`, the live screen
window from `offset` on. Dirty rows are whole-line replaced; colors are
add_regions overlays with scopes served by the dynamic scheme. The ST caret
doubles as the terminal cursor.
"""
import math
from functools import lru_cache

import sublime
import sublime_plugin
from wcwidth import wcswidth

# per-cell call in segment_buffer_line; pyte caches its own wcwidth likewise
cell_width = lru_cache(maxsize=4096)(wcswidth)

from .scheme import scheme_manager
from .terminal import Terminal
from .utils import rev_wcwidth

# zero-width sentinel appended to soft-wrapped lines so copy can re-join them
CONTINUATION = "\u200b\u200c\u200b"

# scrollback colour overlays are merged onto one key per (scope, chunk of rows):
# the key count stays bounded and each merge re-sends at most a chunk's regions
CHUNK = 256


def chunk_key(chunk, scope):
    """Region key of one scrollback chunk's overlays for one scope."""
    return "vterm@{}@{}".format(chunk, scope)


def reverse_fg_bg(fg, bg):
    """Swap the colours of a reverse-video run, keeping "default" resolvable."""
    fg, bg = bg, fg
    if fg == "default":
        fg = "reverse_default"
    if bg == "default":
        bg = "reverse_default"
    return fg, bg


# style flags in the order Char stores them, as the compact scope suffix
STYLE_FLAGS = (("bold", "b"), ("italics", "i"), ("underscore", "u"))

# the Char fields that decide whether a run continues
ATTRS = slice(1, 8)


def is_default_run(fg, bg, style):
    """True for a run that needs no colour overlay."""
    return fg == "default" and bg == "default" and not style


def segment_buffer_line(buffer_line):
    """Yield (text, start, end, fg, bg, style) runs of a screen row.

    A run continues while the cell attributes are unchanged, so a row becomes a
    handful of spans rather than one entry per cell.
    """
    if not buffer_line:
        return
    last_index = max(buffer_line) + 1

    run = []
    start = 0
    column = 0
    attrs = None
    skip_continuation = False

    for i in range(last_index):
        if skip_continuation:
            skip_continuation = False
            continue
        char = buffer_line[i]
        # a wide char owns the next column too, which holds only a filler cell
        skip_continuation = cell_width(char.data) >= 2

        if attrs is None:
            column = i
            attrs = char[ATTRS]
        elif char[ATTRS] != attrs:
            yield _run(run, start, column, attrs)
            run = []
            start = column
            attrs = char[ATTRS]

        # an orphaned wide-char filler (its first half was overwritten) still
        # occupies a column; render a space so later spans stay aligned
        run.append(char.data or " ")
        column += 1

    if attrs is not None:
        yield _run(run, start, column, attrs)


def _run(run, start, end, attrs):
    """Pack one segment: its text, span, resolved colours and style suffix."""
    fg, bg = attrs[0], attrs[1]
    # attrs[6] is Char.reverse
    if attrs[6]:
        fg, bg = reverse_fg_bg(fg, bg)
    style = "".join(flag for i, (_, flag) in enumerate(STYLE_FLAGS, start=2) if attrs[i])
    return "".join(run), start, end, fg, bg, style


def ensure_position(view, edit, row, col=0):
    """Grow the view so (row, col) exists"""
    lastrow = view.rowcol(view.size())[0]
    if lastrow < row:
        view.insert(edit, view.size(), "\n" * (row - lastrow))
    line_region = view.line(view.text_point(row, 0))
    lastcol = view.rowcol(line_region.end())[1]
    if lastcol < col:
        view.insert(edit, line_region.end(), " " * (col - lastcol))


class VtermRenderCommand(sublime_plugin.TextCommand):
    """Draws the screen into the view. Sublime keeps one instance per view, so
    the caches below are that view's render state."""

    def __init__(self, *args, **kwargs):
        """Set up this view's render caches."""
        super().__init__(*args, **kwargs)
        self.colored_lines = {}
        # view row -> (segments, lf) as last rendered; pyte's dirty means
        # "repaint hint", so most dirty rows are identical frame to frame
        self._row_cache = {}
        self._key_counter = 0
        # chunk ordinal -> scopes it holds a key for; ordinals are view row +
        # rows trimmed so far, so they survive trim_history renumbering
        self._archived_chunks = {}
        # rows waiting for their chunk to close, and the chunk they belong to
        self._open_rows = []
        self._open_chunk = None
        self._trimmed = 0
        self._lastrow = 0
        self._applied_cursor_style = None

    def next_key(self):
        """A region key unique to this view; per-row keys would collide after trim_history renumbers rows"""
        self._key_counter += 1
        return "vterm#{}".format(self._key_counter)

    def ensure_row(self, edit, row):
        """Row-existence check for the hot path, tracking the last row in python"""
        if self._lastrow < row:
            self.view.insert(edit, self.view.size(), "\n" * (row - self._lastrow))
            self._lastrow = row

    def run(self, edit):
        """Draw one frame: dirty rows, fold, caret, trimming, title."""
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        screen = terminal.screen
        # the caret and the tab name are the user's; an unfocused terminal leaves
        # both alone so no view event of ours reaches the view they are typing in
        focused = terminal.focused
        # our own replace expands the caret over the text it inserts
        had_selection = any(not region.empty() for region in view.sel())

        if terminal.pending_clear:
            terminal.pending_clear = False
            self.clear_view(edit, terminal)

        if terminal.pending_clear_screen:
            terminal.pending_clear_screen = False
            self.clear_view(edit, terminal, wipe_screen=True)

        if terminal.offset == 0 and view.size() == 0:
            # one blank scrollback row above a fresh screen: sublime has no
            # layout above row 0, and the trackpad poller needs upward slack —
            # without it, trackpad-up is dead until a line retires, which on
            # the alt screen is never
            view.insert(edit, 0, "\n")
            terminal.offset = 1

        self.update_lines(edit, terminal)
        self.sync_alt_fold(terminal)

        if focused and not had_selection:
            # drop the artefact; focus_cursor re-places the caret
            view.sel().clear()

        # all skipped once the user scrolls up, so their place survives the frame
        if following(view, terminal):
            self.trim_trailing_spaces(edit, terminal)
            self.trim_history(edit, terminal)
            if focused:
                # direct call: a nested run_command opens a second edit/undo group
                focus_cursor(view, edit, terminal)
            request_scroll(view, terminal)

        self.sync_cursor_style(terminal)

        if focused:
            title = screen.title or terminal.default_title
            if view.name() != title:
                view.set_name(title)

        # this command is the only writer of screen.dirty, so it owns clearing it
        screen.dirty.clear()

    def sync_alt_fold(self, terminal):
        """Hide the scrollback while the app owns the alt screen: fold it away
        (the view text is its only copy). The scroll range stays: the trackpad
        poller pins the viewport and needs the slack to read the trackpad.
        The fold is re-checked every render — the screen's first row is
        replaced on every region scroll, and an edit at the fold boundary can
        silently disturb a fold."""
        view = self.view
        alt = terminal.screen.alternate_buffer_mode
        if not alt and not terminal.applied_alt_screen:
            return
        entering = alt and not terminal.applied_alt_screen
        terminal.applied_alt_screen = alt
        end = view.text_point(terminal.offset, 0) - 1 if alt and terminal.offset else 0
        want = [(0, end)] if end > 0 else []
        if [(r.begin(), r.end()) for r in view.folded_regions()] == want:
            return
        view.unfold(sublime.Region(0, view.size()))
        if want and view.fold(sublime.Region(0, end)) and entering:
            # entering collapses the layout; a stale viewport anchor would
            # stop the follow-scroll gate from re-engaging. re-asserting a
            # disturbed fold keeps the anchor: apps repaint the boundary row
            # every scroll, and resetting per repair thrashes the poller
            terminal.viewport_y = 0

    def sync_cursor_style(self, terminal):
        """Map DECSCUSR onto the view's caret settings, on transitions only."""
        screen = terminal.screen
        # 0 is the terminal default, which DEC and xterm define as a blinking block
        style = screen.cursor_style or 1
        hidden = screen.cursor_hidden()
        if (style, hidden) == self._applied_cursor_style:
            return
        self._applied_cursor_style = (style, hidden)
        settings = self.view.settings()
        if hidden:
            # the app draws its own cursor, but the caret has to stay for
            # typing to work: a solid block sits on the same cell rather than
            # blinking next to it
            settings.set("block_caret", True)
            settings.set("caret_style", "solid")
        elif 1 <= style <= 6:
            # ST has no underline caret: 3/4 fall back to bar
            settings.set("block_caret", style <= 2)
            settings.set("caret_style", "blink" if style % 2 else "solid")
        else:
            # unknown values: back to the user's caret preferences
            settings.erase("block_caret")
            settings.erase("caret_style")

    def clear_view(self, edit, terminal, wipe_screen=False):
        """Empty the view and scrollback; `wipe_screen` also blanks the screen and asks the app to redraw"""
        view = self.view
        screen = terminal.screen

        view.replace(edit, sublime.Region(0, view.size()), "")
        for line in list(self.colored_lines):
            self.decolorize_line(line)
        for chunk, scopes in self._archived_chunks.items():
            for scope in scopes:
                view.erase_regions(chunk_key(chunk, scope))
        self._archived_chunks.clear()
        self._open_rows = []
        self._open_chunk = None
        self._trimmed = 0
        self._row_cache.clear()
        terminal.offset = 0
        screen.history.clear()
        if wipe_screen:
            screen.wipe()
            terminal.nudge_repaint()
        screen.dirty.update(range(screen.lines))
        terminal.viewport_y = 0

    def update_lines(self, edit, terminal):
        """Write the scrollback that just arrived, then every dirty screen row.

        Lines the emulator retired go in above `terminal.offset`, which advances
        by that many rows; from there down is the live screen.
        """
        screen = terminal.screen
        columns = screen.columns
        dirty_lines = sorted(screen.dirty)
        # valid for this method only; the trim paths erase rows
        self._lastrow = self.view.rowcol(self.view.size())[0]
        if dirty_lines:
            # history pops newest-first, so it fills the rows below `offset` backwards
            history = screen.history
            popped = len(history)
            terminal.offset += popped
            offset = terminal.offset

            for line in range(popped):
                buffer_line = history.pop()
                self.update_line(edit, offset - line - 1, buffer_line, columns)

            # after the loop: a pop edits an earlier point than the one before
            # it, so spans measured mid-loop would be stale
            self.archive_rows(range(offset - popped, offset))

            for line in dirty_lines:
                buffer_line = screen.buffer[line]
                self.update_line(edit, line + offset, buffer_line, columns)

    def update_line(self, edit, line, buffer_line, columns):
        """Rewrite one view row from a screen row, unless it is unchanged.

        Args:
            line: absolute view row.
            columns: screen width; the last cell's linefeed flag marks a soft
                wrap, which gets the CONTINUATION sentinel.
        """
        view = self.view
        lf = buffer_line[columns - 1].linefeed
        segments = list(segment_buffer_line(buffer_line))
        # pyte's dirty set is a repaint hint, not a diff: most dirty rows come
        # back identical, and rewriting one costs an edit plus its region churn
        rendered = (segments, lf)
        if self._row_cache.get(line) == rendered:
            return
        self._row_cache[line] = rendered

        by_scope = {}
        needed = 0
        for _, start, end, fg, bg, style in segments:
            if is_default_run(fg, bg, style):
                continue
            needed = max(needed, end)
            by_scope.setdefault(scheme_manager.scope(fg, bg, style), []).append((start, end))

        text = "".join(s[0] for s in segments)
        if lf:
            text += CONTINUATION
        text = text.rstrip()
        if len(text) < needed:
            # pad only to the last coloured run, in the same edit as the text
            text += " " * (needed - len(text))

        self.ensure_row(edit, line)
        line_region = view.line(view.text_point(line, 0))
        self.decolorize_line(line)
        view.replace(edit, line_region, text)
        # replacing a line cannot move its start
        self.colorize_line(line, line_region.begin(), by_scope)

    def colorize_line(self, line, line_start, by_scope):
        """Lay the colour overlays for one row, one add_regions per scope."""
        view = self.view
        keys = self.colored_lines.setdefault(line, []) if by_scope else None
        for scope, spans in by_scope.items():
            key = self.next_key()
            view.add_regions(key, [sublime.Region(line_start + a, line_start + b) for a, b in spans], scope)
            keys.append(key)

    def decolorize_line(self, line):
        """Drop a row's colour overlays before it is rewritten or erased."""
        if line in self.colored_lines:
            for key in self.colored_lines[line]:
                self.view.erase_regions(key)
            del self.colored_lines[line]

    def archive_rows(self, rows):
        """Queue rows that left the screen for their chunk's merge."""
        for row in rows:
            if row not in self.colored_lines:
                continue
            chunk = (row + self._trimmed) // CHUNK
            if chunk != self._open_chunk:
                self.flush_chunk()
                self._open_chunk = chunk
            self._open_rows.append(row)

    def flush_chunk(self):
        """Merge the queued rows onto shared per-(scope, chunk) keys.

        A scrollback row is never repainted, so its per-row keys used to live
        until trimmed: a long coloured session held ~15k region keys, and every
        key is a separate pass in sublime's redraw. Merging bounds the count at
        scopes x (scrollback / CHUNK).

        The merge waits for the chunk to close, because add_regions replaces a
        key's whole set: merging every frame re-sent everything accumulated so
        far, which is quadratic in CHUNK when output arrives a few rows a frame.
        Until then the rows keep their per-row keys, so the count is bounded by
        scopes x chunks plus one chunk's worth.
        """
        view = self.view
        rows, chunk = self._open_rows, self._open_chunk
        self._open_rows = []
        if not rows:
            return

        grouped = {}
        stale = []
        for row in rows:
            keys = self.colored_lines.pop(row, None)
            if not keys:
                continue
            stale += keys
            # the segments that were painted, straight from the row cache
            segments = self._row_cache[row][0]
            start = view.line(view.text_point(row, 0)).begin()
            for _, a, b, fg, bg, style in segments:
                if is_default_run(fg, bg, style):
                    continue
                scope = scheme_manager.scope(fg, bg, style)
                grouped.setdefault(scope, []).append(sublime.Region(start + a, start + b))

        scopes = self._archived_chunks.setdefault(chunk, set())
        for scope, regions in grouped.items():
            key = chunk_key(chunk, scope)
            if scope in scopes:
                # a trim flushed this chunk mid-way; keep what it already holds
                regions = view.get_regions(key) + regions
            view.add_regions(key, regions, scope)
            scopes.add(scope)
        for key in stale:
            view.erase_regions(key)

    def trim_trailing_spaces(self, edit, terminal):
        """Erase the blank tail below the cursor, and padding on the cursor row.

        Without this the view keeps a full screen of blank rows below a short
        prompt, and the scrollbar reflects them.
        """
        view = self.view
        screen = terminal.screen
        cursor = screen.cursor
        cursor_row = terminal.offset + cursor.y
        lastrow = view.rowcol(view.size())[0]
        row = lastrow
        while row > cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            if len(text.strip()) == 0 and (row not in self.colored_lines or not self.colored_lines[row]):
                row = row - 1
            else:
                break
        if row < lastrow:
            # one erase for the whole blank tail; the row cache entries stay,
            # so a still-blank row is not recreated and re-erased every tick
            view.erase(edit, sublime.Region(view.text_point(row + 1, 0) - 1, view.size()))
        if row == cursor_row:
            line_region = view.line(view.text_point(row, 0))
            text = view.substr(line_region)
            trailing_region = sublime.Region(line_region.begin() + rev_wcwidth(text, cursor.x) + 1, line_region.end())
            if not trailing_region.empty() and len(view.substr(trailing_region).strip()) == 0:
                view.erase(edit, trailing_region)
                self._row_cache.pop(row, None)

    def trim_history(self, edit, terminal):
        """Hold the view to scrollback_size rows, and drop rows below the screen."""
        view = self.view
        screen = terminal.screen
        lastrow = view.rowcol(view.size())[0]
        n = terminal.scrollback_size
        if lastrow + 1 > n:
            # queued rows are about to be renumbered or erased: merge them first
            self.flush_chunk()
            m = max(lastrow + 1 - n, math.ceil(n / 10))
            for line in range(m):
                self.decolorize_line(line)
            self.colored_lines = {k - m: v for (k, v) in self.colored_lines.items()}
            self._row_cache = {k - m: v for (k, v) in self._row_cache.items() if k >= m}
            top_region = sublime.Region(0, view.full_line(view.text_point(m - 1, 0)).end())
            view.erase(edit, top_region)
            terminal.offset -= m
            lastrow -= m

            # chunks wholly above the cut go; the boundary chunk drops the
            # regions the erase collapsed to empty
            self._trimmed += m
            cut = self._trimmed // CHUNK
            for chunk in [c for c in self._archived_chunks if c < cut]:
                for scope in self._archived_chunks.pop(chunk):
                    view.erase_regions(chunk_key(chunk, scope))
            for scope in self._archived_chunks.get(cut, ()):
                key = chunk_key(cut, scope)
                view.add_regions(key, [r for r in view.get_regions(key) if not r.empty()], scope)

        if lastrow > terminal.offset + screen.lines:
            tail_region = sublime.Region(view.text_point(terminal.offset + screen.lines, 0), view.size())
            for line in view.lines(tail_region):
                row = view.rowcol(line.begin())[0]
                self.decolorize_line(row)
                self._row_cache.pop(row, None)
            view.erase(edit, tail_region)


def focus_cursor(view, edit, terminal):
    """Put the caret where the terminal cursor is, keeping any user selection.

    The caret is placed even when the app hides the cursor (CSI ?25l): sublime
    drops a plain space keypress in a view that has no caret, and IME text has
    no insertion point. sync_cursor_style turns it into a block there, so it
    lands on the cell the app draws its own cursor over, not beside it.
    """
    sel = view.sel()
    # keep a user selection; typing collapses it (VtermKeypressCommand)
    if any(not region.empty() for region in sel):
        return

    sel.clear()

    screen = terminal.screen
    cursor = screen.cursor
    offset = terminal.offset

    ensure_position(view, edit, cursor.y + offset)

    line_region = view.line(view.text_point(cursor.y + offset, 0))
    text = view.substr(line_region)
    col = rev_wcwidth(text, cursor.x) + 1

    ensure_position(view, edit, cursor.y + offset, col)
    pt = view.text_point(cursor.y + offset, col)

    sel.add(sublime.Region(pt, pt))


def following(view, terminal):
    """Whether the viewport still sits where the last auto-scroll left it; above
    that means the user scrolled up by hand."""
    return terminal.viewport_y < view.viewport_position()[1] + view.line_height()


def bottom_y(view, terminal):
    """Viewport y that pins the bottom of the live screen to the bottom of the view."""
    # layout_extent is maintained incrementally; text_to_layout(size()) forced
    # a whole-buffer layout on every call
    viewport_y = view.layout_extent()[1] - view.viewport_extent()[1]
    offset_y = view.text_to_layout(view.text_point(terminal.offset, 0))[1]
    return max(offset_y, viewport_y)


def scroll_to_cursor(view, terminal):
    """Follow the output: pin the viewport to the bottom of the live screen."""
    terminal._scroll_pending = False
    y = bottom_y(view, terminal)
    if y == terminal.viewport_y and terminal.trackpad_pinning():
        # the anchor is unchanged and the trackpad poller owns the pin:
        # re-pinning here would discard displacement it has not harvested yet,
        # and apps repaint while scrolling, so this races every gesture
        return
    terminal.viewport_y = y
    view.set_viewport_position((0, y), False)


def scroll_view(view, terminal, up, lines):
    """Move the viewport `lines` rows over the scrollback.

    Not sublime's scroll_lines: it animates, and while the viewport is still
    within a line of where it started the follow-scroll gate reads that as "at
    the bottom" and pulls it back down.
    """
    delta = lines * view.line_height()
    y = view.viewport_position()[1]
    y = max(y - delta, 0) if up else min(y + delta, bottom_y(view, terminal))
    view.set_viewport_position((0, y), False)


def request_scroll(view, terminal):
    """Coalesce follow-scrolls: at most one queued callback per terminal."""
    if not terminal._scroll_pending:
        terminal._scroll_pending = True
        sublime.set_timeout(lambda: scroll_to_cursor(view, terminal))


class VtermShowCursorCommand(sublime_plugin.TextCommand):
    """Places the caret at the terminal cursor. For callers outside a render,
    which has no edit token of its own."""

    def run(self, edit, scroll=True):
        """Args:
        scroll: also follow the output down to the cursor.
        """
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        focus_cursor(view, edit, terminal)
        if scroll:
            request_scroll(view, terminal)
