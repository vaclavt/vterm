""" SGR mouse reporting while the app tracks the mouse; otherwise sublime keeps drag_select """
import sublime
import sublime_plugin

from .render import following
from .terminal import Terminal

# SGR button numbers for the wheel; 64 is the "scroll" bit plus direction
WHEEL_UP = 64
WHEEL_DOWN = 65

# lines per wheel tick, as iTerm2 delivers per notch; pi scrolls exactly one
# line per report and has no setting of its own
WHEEL_LINES = 3

# trackpad poll period: one frame at 60 Hz, so a snapped-back displacement
# never exceeds a frame's worth of motion
TRACKPAD_POLL_MS = 16


def send_scroll(terminal, up, lines, cell=None):
    """Report `lines` scroll lines to the app, or fake arrow keys on the alt screen.

    One report per line, never one batched write: pi matches the SGR form
    against a whole stdin chunk and drops anything longer."""
    if terminal.screen.mouse_reporting_enabled():
        if cell is None:
            cell = (1, terminal.screen.lines // 2 + 1)
        for _ in range(lines):
            terminal.send_mouse(WHEEL_UP if up else WHEEL_DOWN, cell[0], cell[1], pressed=True)
    elif terminal.screen.alternate_buffer_mode:
        # alternate scroll: an alt-screen app that does not track the mouse
        # gets arrow keys per wheel tick, as kitty/iTerm2/xterm do
        for _ in range(lines):
            terminal.send_key(key="up" if up else "down")


def _event_to_cell(view, terminal, event, clamp=False):
    """Map a mouse event to 1-based (col, row) on the live screen, or None.

    `clamp` pulls a point above or below the screen onto the nearest row: the
    wheel still belongs to the app, while a click there does not."""
    pt = view.window_to_text((event["x"], event["y"]))
    row, col = view.rowcol(pt)
    row -= terminal.offset
    screen = terminal.screen
    if row < 0 or row >= screen.lines:
        if not clamp:
            return None
        row = min(max(row, 0), screen.lines - 1)
    return (min(col, screen.columns - 1) + 1, row + 1)


class VtermMouseEventListener(sublime_plugin.EventListener):
    """Hands button-1 clicks to the app, but only while it tracks the mouse."""

    def on_text_command(self, view, name, args):
        """Replace drag_select with a mouse report; other click variants stay native."""
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        if not terminal.screen.mouse_reporting_enabled():
            # leave selection, caret and scrolling to sublime
            return
        if name == "drag_select" and args and args.get("event", {}).get("button") == 1 and len(args) == 1:
            return ("vterm_mouse_click", {"event": args["event"]})


class VtermMouseClickCommand(sublime_plugin.TextCommand):
    """Reports a click to the app, or focuses the tab if it was not active."""

    def run(self, edit, event):
        """Focus the tab on the first click, report press and release after that."""
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        window = self.view.window()
        if window and window.active_view() != self.view:
            # we replaced drag_select, which is what focuses the group; this
            # click only focuses, as clicking an unfocused terminal window does
            window.focus_view(self.view)
            return
        cell = _event_to_cell(self.view, terminal, event)
        if cell is None:
            return
        terminal.send_mouse(0, cell[0], cell[1], pressed=True)
        terminal.send_mouse(0, cell[0], cell[1], pressed=False)

    def want_event(self):
        """Ask sublime for the click coordinates."""
        return True


class VtermScrollCommand(sublime_plugin.TextCommand):
    """Bound to scroll wheel in the mousemap while the app tracks the mouse or is on the alt screen."""

    def run(self, edit, event=None, up=False):
        """Report one wheel notch to the app.

        Only bound while the app owns the wheel; otherwise sublime scrolls the
        view over the real scrollback text and this never runs.
        """
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        cell = None
        if terminal.screen.mouse_reporting_enabled() and event:
            cell = _event_to_cell(self.view, terminal, event, clamp=True)
        send_scroll(terminal, up, WHEEL_LINES, cell)

    def want_event(self):
        """Ask sublime for the wheel coordinates."""
        return True


def start_trackpad(terminal):
    """Start the poller that turns trackpad scrolling into wheel lines.

    The trackpad never fires the mousemap scroll bindings on macOS
    (sublime_text#1838); its only trace is viewport movement. While the app
    owns the wheel the mousemap keeps the physical wheel off the viewport, so
    any displacement from the follow anchor is trackpad scroll: harvest whole
    lines from it, send them like wheel lines, and snap the viewport back.

    The caller checks _trackpad_running; the first tick arms the baseline."""
    terminal._trackpad_running = True
    terminal._trackpad_engaged = True
    terminal._trackpad_baseline = None
    sublime.set_timeout(lambda: _trackpad_tick(terminal), TRACKPAD_POLL_MS)


def _trackpad_tick(terminal):
    """One poll: harvest viewport displacement, reschedule while captured."""
    view = terminal.view
    if Terminal.from_id(view.id()) is not terminal or not terminal.alive \
            or not terminal._wheel_captured or not terminal.is_visible():
        terminal._trackpad_running = False
        return
    anchor = terminal.viewport_y
    extent = view.viewport_extent()[1]
    if (anchor, extent) != terminal._trackpad_baseline:
        # the renderer moved the anchor (follow-scroll, fold, resize): re-arm
        # around the new position instead of reading the move as a gesture
        terminal._trackpad_baseline = (anchor, extent)
        terminal._trackpad_acc = 0.0
    elif not terminal._trackpad_engaged:
        if following(view, terminal):
            terminal._trackpad_engaged = True
            terminal._trackpad_acc = 0.0
    else:
        dy = view.viewport_position()[1] - anchor
        if dy == 0:
            # at rest a stale fraction must not fire on the next gesture
            terminal._trackpad_acc = 0.0
        elif abs(dy) >= extent:
            # scrollbar/minimap jump or page scroll, not a trackpad gesture:
            # let the user browse until they return to the anchor
            terminal._trackpad_engaged = False
        else:
            terminal._trackpad_acc += dy / view.line_height()
            lines = int(terminal._trackpad_acc)
            terminal._trackpad_acc -= lines
            if lines:
                send_scroll(terminal, up=lines < 0, lines=abs(lines))
            view.set_viewport_position((0, anchor), False)
    sublime.set_timeout(lambda: _trackpad_tick(terminal), TRACKPAD_POLL_MS)
