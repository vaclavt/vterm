""" SGR mouse reporting while the app tracks the mouse; otherwise sublime keeps drag_select

The trackpad is not handled here. On macOS it never reaches a mousemap binding
(sublime_text#1838), so it needs a utility such as LinearMouse to convert its
continuous scrolling into discrete wheel events; those arrive as ordinary wheel
notches and take the path below.
"""
import sublime_plugin

from .terminal import Terminal

# SGR button numbers for the wheel; 64 is the "scroll" bit plus direction
WHEEL_UP = 64
WHEEL_DOWN = 65

# lines per wheel tick, as iTerm2 delivers per notch
WHEEL_LINES = 3


def send_scroll(terminal, up, lines, cell=None):
    """Report `lines` scroll lines to the app, or fake arrow keys on the alt screen.

    One report per write, never a batch: an app may match the SGR form against
    a whole stdin chunk and drop anything longer."""
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

    Clamping pulls a point above or below the screen onto the nearest row: the
    wheel still belongs to the app there, a click does not."""
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
