""" User-facing commands and the terminal view setup """
import logging
import os
import subprocess

import sublime
import sublime_plugin

from .render import CONTINUATION, scroll_view
from .scheme import get_scheme_manager
from .terminal import Terminal
from .themes import ALL_THEME_NAMES, THEMES
from .utils import SETTINGS_FILE, settings

LOG = logging.getLogger(__name__)

# nav keys the view may consume from the app, with the modifier configured
SCROLL_KEYS = ("pageup", "pagedown", "up", "down")


def editor_view(window):
    """A view to sample the editor's colours from: never a terminal, whose
    colours are its own theme's."""
    return next((v for v in window.views() if not v.settings().get("vterm_view")), None)


class VtermOpenCommand(sublime_plugin.WindowCommand):
    """Open a terminal in a new tab. `vterm_open`, the only entry point users bind."""

    def run(self, cmd=None, cwd=None, env=None, title=None):
        """Start `cmd` (default: a login shell) in a fresh view"""
        window = self.window

        if not cmd:
            shell = os.environ.get("SHELL", "/bin/zsh")
            cmd = [shell, "-l"]
        elif isinstance(cmd, str):
            cmd = [cmd]

        # cwd: the first project folder, else the active file's directory, else home
        if not cwd:
            folders = window.folders()
            view = window.active_view()
            if folders:
                cwd = folders[0]
            elif view and view.file_name():
                cwd = os.path.dirname(view.file_name())
            else:
                cwd = os.path.expanduser("~")
        cwd = sublime.expand_variables(cwd, window.extract_variables())

        scheme = get_scheme_manager(settings().get("theme"), host_view=editor_view(window))

        view = window.new_file()
        view.run_command("vterm_initialize_view", {"color_scheme": scheme.path})

        terminal = Terminal(view, scheme)
        try:
            terminal.start(cmd, cwd=cwd, env=env, default_title=title or os.path.basename(cmd[0]))
        except Exception:
            LOG.exception("failed to start terminal: cmd=%s", cmd)
            view.close()
            sublime.error_message("vterm: could not start {}".format(" ".join(cmd)))
            return
        window.focus_view(view)


class VtermOpenTerminalProjectFolderCommand(sublime_plugin.WindowCommand):
    """Open the external terminal in the project folder of the active file."""

    def run(self):
        """Resolve the folder, substitute %CWD%, spawn the configured terminal."""
        folders = self.window.folders()
        if not folders:
            return
        cwd = folders[0]
        view = self.window.active_view()
        if view and view.file_name():
            path = view.file_name()
            matches = [f for f in folders if path.find(f.rstrip(os.sep) + os.sep) == 0]
            if matches:
                cwd = max(matches, key=len)

        s = settings()
        terminal = s.get("terminal")
        if not terminal:
            sublime.error_message('vterm: set "terminal" in vterm.sublime-settings')
            return

        parameters = s.get("parameters", [])
        if not isinstance(parameters, list) or not all(isinstance(p, str) for p in parameters):
            sublime.error_message('vterm: "parameters" must be a list of strings')
            return
        parameters = [p.replace("%CWD%", cwd) for p in parameters]
        try:
            subprocess.Popen(
                [terminal] + parameters,
                cwd=cwd,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            LOG.exception("failed to start external terminal: terminal=%s", terminal)
            sublime.error_message("vterm: could not start {}".format(terminal))

    def is_visible(self):
        """Hide the palette entry when there is no project folder."""
        return bool(self.window.folders())


class VtermInitializeViewCommand(sublime_plugin.TextCommand):
    """Stamps a fresh view with the settings that make it a terminal."""

    def run(self, _, color_scheme):
        """Apply the view settings; the pty starts only after this."""
        view = self.view
        view_settings = view.settings()
        view_settings.set("vterm_view", True)
        view.set_scratch(True)
        view.set_read_only(False)
        view_settings.set("is_widget", True)
        view_settings.set("gutter", False)
        view_settings.set("highlight_line", False)
        view_settings.set("auto_complete_commit_on_tab", False)
        view_settings.set("draw_centered", False)
        view_settings.set("word_wrap", False)
        view_settings.set("auto_complete", False)
        view_settings.set("draw_white_space", "none")
        view_settings.set("draw_unicode_white_space", False)
        view_settings.set("draw_indent_guides", False)
        view_settings.set("scroll_past_end", False)
        view_settings.set("scroll_context_lines", 0)
        view_settings.set("color_scheme", color_scheme)
        view_settings.set("syntax", "Packages/vterm/vterm.sublime-syntax")
        view_settings.set("spell_check", False)
        view_settings.set("detect_indentation", False)
        view_settings.set("translate_tabs_to_spaces", False)
        view_settings.set("command_mode", False)


class VtermKeypressCommand(sublime_plugin.TextCommand):
    """Sends one key to the app. Every key binding in the generated keymap lands here."""

    def run(self, _, **kwargs):
        """Send one key; the name and modifier flags come straight from the keymap"""
        terminal = Terminal.from_id(self.view.id())
        if not terminal or not terminal.alive:
            return
        if self.view_scrolled(terminal, **kwargs):
            return
        # drop any selection so the caret follows the terminal cursor
        self.view.sel().clear()
        self.view.run_command("vterm_show_cursor")
        terminal.send_key(**kwargs)

    def view_scrolled(self, terminal, key=None, **mods):
        """Whether the view consumed the key by scrolling over the scrollback.

        Runs before vterm_show_cursor, whose scroll would pull the viewport
        straight back down. PgUp/PgDn move a screen, Up/Down one line. On the
        alt screen the scrollback is folded away, so the app keeps the key.
        """
        # the cheap tests first: this runs on every keystroke
        if key not in SCROLL_KEYS or not mods or terminal.screen.alternate_buffer_mode:
            return False
        modifier = settings().get("scroll_modifier", "shift")
        # exact match: ctrl+shift+pageup still belongs to the app
        if not modifier or {m for m, on in mods.items() if on} != set(modifier.split("+")):
            return False
        lines = 1 if key in ("up", "down") else terminal.screen.lines
        scroll_view(self.view, terminal, up=key in ("up", "pageup"), lines=lines)
        return True


def selection_text(view):
    """The view's selection as one string, with soft-wrapped rows re-joined."""
    text = "\n".join(view.substr(s) for s in view.sel())
    return text.replace(CONTINUATION + "\n", "").replace(CONTINUATION, "")


class VtermCopyCommand(sublime_plugin.TextCommand):
    """Copies the selection, re-joining rows the emulator soft-wrapped."""

    def run(self, edit):
        """Strip the soft-wrap sentinels, then copy as usual."""
        if not self.view.settings().get("vterm_view"):
            return
        sublime.set_clipboard(selection_text(self.view))


class VtermPasteTextCommand(sublime_plugin.TextCommand):
    """Sends arbitrary text to the app, bracketed when it asked for that."""

    def run(self, edit, text, bracketed=True):
        """Send text to the app, wrapped in paste brackets when it enabled ?2004"""
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return

        bracketed = bracketed and terminal.screen.bracketed_paste_enabled()
        if bracketed:
            terminal.send_key(key="bracketed_paste_start")

        self.view.run_command("vterm_show_cursor")
        terminal.send_string(text)

        if bracketed:
            terminal.send_key(key="bracketed_paste_end")


class VtermPasteSelectionCommand(sublime_plugin.TextCommand):
    """Sends the view's own selection to the app; the middle button, X11-style."""

    def run(self, edit):
        """Bracketed like any paste, and the clipboard is never touched."""
        text = selection_text(self.view)
        if not text:
            return
        # as typing does, so the caret follows the terminal cursor again
        self.view.sel().clear()
        self.view.run_command("vterm_paste_text", {"text": text})


class VtermSelectThemeCommand(sublime_plugin.WindowCommand):
    """Retheme the focused terminal, previewing each choice as it is highlighted.

    The chosen theme also becomes the default for terminals opened from now on;
    it does not touch any other already-open terminal.
    """

    def run(self):
        """Show the palette picker, starting on the focused terminal's own theme."""
        self.terminal = self._focused_terminal()
        if not self.terminal:
            return
        self.original = self.terminal.scheme.theme_name
        self.choices = ALL_THEME_NAMES

        items = [["No theme", "follow the Sublime color scheme"]]
        for name in self.choices[1:]:
            theme = THEMES[name]
            items.append([name, "{} on {}".format(theme["foreground"], theme["background"])])

        current = self.choices.index(self.original) if self.original in self.choices else 0
        self.window.show_quick_panel(items, self._done, selected_index=current, on_highlight=self._preview)

    def is_enabled(self):
        """Greys the palette entry out when the focused view is not a terminal."""
        return self._focused_terminal() is not None

    def _focused_terminal(self):
        """The terminal in the focused view, if any."""
        view = self.window.active_view()
        return Terminal.from_id(view.id()) if view else None

    def _apply(self, name):
        """Retheme the focused terminal alone."""
        scheme = get_scheme_manager(name, host_view=editor_view(self.window))
        self.terminal.retheme(scheme)

    def _preview(self, index):
        """Apply a palette to the focused terminal while it is highlighted."""
        self._apply(self.choices[index])

    def _done(self, index):
        """On cancel, restore the focused terminal's original theme.

        On confirm, keep the focused terminal on its new theme and persist the
        choice as the default for terminals opened from now on.
        """
        if index == -1:
            self._apply(self.original)
            return
        name = self.choices[index]
        self._apply(name)
        settings().set("theme", name)
        sublime.save_settings(SETTINGS_FILE)


class VtermClearScreenCommand(sublime_plugin.TextCommand):
    """Clear the view, the screen and all scrollback, then let the app redraw"""

    def run(self, edit):
        """Flag the clear; the renderer applies it and asks the app to redraw."""
        terminal = Terminal.from_id(self.view.id())
        if not terminal:
            return
        # the renderer thread owns the screen; it applies this on its next tick
        terminal.pending_clear_screen = True
        terminal.wake()
        self.view.run_command("vterm_clear_undo_stack")

    def is_enabled(self):
        """Greys the palette entry out for views that are not terminals."""
        return Terminal.from_id(self.view.id()) is not None


class VtermCleanupCommand(sublime_plugin.TextCommand):
    """Finishes a terminal whose process exited: last frame, then close or annotate."""

    def run(self, edit):
        """Render the last frame, then close the tab or leave the exit code behind."""
        view = self.view
        terminal = Terminal.from_id(view.id())
        if not terminal:
            return

        # latched: the flag also disables the escape binding for a dead tab
        if view.settings().get("vterm_view.finished"):
            return
        view.settings().set("vterm_view.finished", True)

        view.run_command("vterm_render")
        terminal.kill()

        if settings().get("auto_close", True):
            view.close()
            return

        # reaps the child, which sets exitstatus
        terminal.process.isalive()
        exitstatus = terminal.process.exitstatus
        if exitstatus is not None:
            view.run_command("append", {"characters": "\n[process exited with code {}]".format(exitstatus)})
        view.sel().clear()


class VtermClearUndoStackCommand(sublime_plugin.TextCommand):
    """Drops the undo history, so rendered output can never be undone into view."""

    def run(self, _):
        """Deferred: clearing during a command would fight the edit in progress."""
        sublime.set_timeout(self.view.clear_undo_stack)


class VtermNoopCommand(sublime_plugin.TextCommand):
    """Swallows a sublime command that must not reach a terminal view."""

    def run(self, _):
        """Deliberately does nothing."""
        pass
