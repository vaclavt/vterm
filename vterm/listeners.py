""" Event listeners: printable/IME input capture, command rerouting, keymap context, focus reporting, lifecycle """
import sublime
import sublime_plugin

from .terminal import Terminal


class VtermCoreEventListener(sublime_plugin.EventListener):
    """View events a terminal has to answer: focus, input capture, teardown.

    Application-wide, so every hook runs for ordinary files too and has to bail
    out cheaply.
    """

    def __init__(self):
        # view id -> last caret position, to work out what sublime just inserted
        self._cursor = {}

    def on_activated(self, view):
        """Report focus; warn if the plugin host restarted and left the tab orphaned."""
        terminal = Terminal.from_id(view.id())
        if terminal:
            terminal.focused = True
            terminal.send_focus(True)
            view.run_command("vterm_clear_undo_stack")
            # catch up on the frames, title and caret skipped while in the
            # background — via the renderer tick: a render here would race the
            # renderer thread, which owns the emulator
            terminal.pending_render = True
            terminal.wake()
            view.run_command("vterm_show_cursor", {"scroll": False})
        elif view.settings().get("vterm_view") and not view.settings().get("vterm_view.finished"):
            # the plugin host restarted; this tab's terminal is unreachable
            view.set_status("vterm", "vterm: disconnected — close this tab and open a new one")

    def on_deactivated(self, view):
        """Report focus loss, and leave the view without a caret while it is in the background."""
        terminal = Terminal.from_id(view.id())
        if terminal:
            terminal.focused = False
            terminal.send_focus(False)
            if all(region.empty() for region in view.sel()):
                view.sel().clear()

    def on_pre_close(self, view):
        """Kill the process when its tab closes."""
        terminal = Terminal.from_id(view.id())
        if terminal:
            terminal.kill()
        self._cursor.pop(view.id(), None)

    def on_modified(self, view):
        """Forward text sublime inserted, and undo anything else that edits the view.

        The keymap covers printable keys, but IME and composed input never
        raise a key event — it arrives as an `insert` command. Text the emulator
        did not produce must never stay in the view.
        """
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return
        command, args, _ = view.command_history(0)
        if command.startswith("vterm"):
            return
        if not terminal.alive:
            return
        if command == "insert" and "characters" in args and len(view.sel()) == 1 and view.sel()[0].empty():
            chars = args["characters"]
            current_cursor = view.sel()[0].end()
            prev = self._cursor.get(view.id(), 0)
            region = sublime.Region(max(current_cursor - len(chars), prev), current_cursor)
            text = view.substr(region)
            self._cursor[view.id()] = current_cursor
            view.run_command("vterm_paste_text", {"text": text, "bracketed": False})
        elif command:
            view.run_command("soft_undo")

    def on_selection_modified(self, view):
        """Track the caret, so on_modified can tell which text was just inserted."""
        terminal = Terminal.from_id(view.id())
        if not terminal or not terminal.alive:
            return
        if len(view.sel()) != 1 or not view.sel()[0].empty():
            return
        self._cursor[view.id()] = view.sel()[0].end()

    def on_query_context(self, view, key, operator, operand, match_all):
        """Answer the contexts the mousemap needs"""
        # keymap contexts are native "setting.x"; mousemaps do not evaluate
        # those the same way, so their flags still need the hook
        if key not in ("vterm_view", "vterm_view.wheel_captured", "vterm_view.mouse_reporting"):
            return None
        value = view.settings().get(key, False)
        if operator == sublime.OP_EQUAL:
            return value == operand
        elif operator == sublime.OP_NOT_EQUAL:
            return value != operand
        return None

    def on_text_command(self, view, name, args):
        """Reroute copy/paste to the pty and swallow undo, in terminal views."""
        if not view.settings().get("vterm_view"):
            return
        if name == "copy":
            return ("vterm_copy", None)
        elif name in ("paste", "paste_and_indent"):
            return ("vterm_paste_text", {"text": sublime.get_clipboard()})
        elif name == "undo":
            return ("vterm_noop", None)
