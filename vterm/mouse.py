""" SGR mouse reporting while the app tracks the mouse; otherwise sublime keeps drag_select

The trackpad is not handled here. On macOS it never reaches a mousemap binding
(sublime_text#1838), so it needs a utility such as LinearMouse to convert its
continuous scrolling into discrete wheel events; those arrive as ordinary wheel
notches and take the path below.
"""
import sublime_plugin

from .terminal import Terminal
from .utils import settings

# SGR button numbers for the wheel; 64 is the "scroll" bit plus direction
WHEEL_UP = 64
WHEEL_DOWN = 65
# button 0 with the "moved while held" bit
MOTION = 32


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
    wheel and a release still belong to the app there, a press does not."""
    pt = view.window_to_text((event["x"], event["y"]))
    row, col = view.rowcol(pt)
    row -= terminal.offset
    screen = terminal.screen
    if row < 0 or row >= screen.lines:
        if not clamp:
            return None
        row = min(max(row, 0), screen.lines - 1)
    return (min(col, screen.columns - 1) + 1, row + 1)


class VtermMousePressCommand(sublime_plugin.TextCommand):
    """Bound to button 1 in the mousemap while the app tracks the mouse."""

    def run(self, edit, event):
        """Focus an inactive tab; else report the press and remember its cell."""
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        terminal.mouse_press = None  # a release may not come: the context can drop mid-drag
        window = self.view.window()
        if window and window.active_view() != self.view:
            # the mousemap binding displaces drag_select, which is what focuses
            # the group; this click only focuses, as in a terminal window
            window.focus_view(self.view)
            return
        cell = _event_to_cell(self.view, terminal, event)
        if cell is None:
            return
        terminal.mouse_press = cell
        terminal.send_mouse(0, cell[0], cell[1], pressed=True)
        # native drag_select only previews the selection while dragging; the
        # app gets the real one at release
        self.view.run_command("drag_select", {"event": event})

    def want_event(self):
        """Ask sublime for the click coordinates."""
        return True


class VtermMouseReleaseCommand(sublime_plugin.TextCommand):
    """Reports the release, after one drag motion when the button came up elsewhere.

    Sublime raises no motion events, so a drag reaches the app as a single
    move to the release cell; an app's selection then spans press to release.
    The native preview selection goes, leaving the app's own highlight."""

    def run(self, edit, event):
        """Report motion and release for the pending press, if any."""
        terminal = Terminal.from_id(self.view.id())
        if not terminal or terminal.mouse_press is None:
            return
        cell = _event_to_cell(self.view, terminal, event, clamp=True)
        if cell != terminal.mouse_press:
            terminal.send_mouse(MOTION, cell[0], cell[1], pressed=True)
        terminal.send_mouse(0, cell[0], cell[1], pressed=False)
        terminal.mouse_press = None
        self.view.sel().clear()
        self.view.run_command("vterm_show_cursor", {"scroll": False})

    def want_event(self):
        """Ask sublime for the release coordinates."""
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
        lines = max(0, int(settings().get("wheel_lines", 1)))
        send_scroll(terminal, up, lines, cell)

    def want_event(self):
        """Ask sublime for the wheel coordinates."""
        return True
