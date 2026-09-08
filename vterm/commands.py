""" User-facing commands and the terminal view setup """
import logging
import os
import subprocess

import sublime
import sublime_plugin

from .render import CONTINUATION, scroll_page
from .scheme import SCHEME_PATH, scheme_manager
from .terminal import Terminal
from .themes import THEMES
from .utils import SETTINGS_FILE, settings

LOG = logging.getLogger(__name__)


class VtermOpenCommand(sublime_plugin.WindowCommand):
    """Open a terminal in a new tab. `vterm_open`, the only entry point users bind."""

    def run(self, cmd=None, cwd=None, env=None, title=None):
        """Start `cmd` (default: a login shell) in a fresh view.

        Args:
            cwd: defaults to the first project folder, else the active file's
                directory, else the home directory.
            env: extra environment on top of the child defaults.
            title: tab name; the program name unless the app sets its own.
        """
        window = self.window

        if not cmd:
            shell = os.environ.get("SHELL", "/bin/zsh")
            cmd = [shell, "-l"]
        elif isinstance(cmd, str):
            cmd = [cmd]

        # the app may override the title via OSC 0/2 later
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

        scheme_manager.initialize(window.active_view())

        view = window.new_file()
        view.run_command("vterm_initialize_view")

        terminal = Terminal(view)
        try:
            terminal.start(cmd, cwd=cwd, env=env, default_title=title or os.path.basename(cmd[0]))
        except Exception:
            LOG.exception("failed to start terminal: cmd=%s", cmd)
            view.close()
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
                [terminal] + parameters, cwd=cwd, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            LOG.exception("failed to start external terminal: terminal=%s", terminal)
            sublime.error_message("vterm: could not start {}".format(terminal))

    def is_visible(self):
        """Hide the palette entry when there is no project folder."""
        return bool(self.window.folders())


class VtermInitializeViewCommand(sublime_plugin.TextCommand):
    """Stamps a fresh view with the settings that make it a terminal."""

    def run(self, _):
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
        view_settings.set("scroll_past_end", True)
        view_settings.set("scroll_context_lines", 0)
        view_settings.set("color_scheme", SCHEME_PATH)
        view_settings.set("syntax", "Packages/vterm/vterm.sublime-syntax")
        view_settings.set("spell_check", False)
        view_settings.set("detect_indentation", False)
        view_settings.set("translate_tabs_to_spaces", False)
        view_settings.set("command_mode", False)


class VtermKeypressCommand(sublime_plugin.TextCommand):
    """Sends one key to the app. Every key binding in the generated keymap lands here."""

    def run(self, _, **kwargs):
        """Args:
        kwargs: key name plus modifier flags, straight from the keymap.
        """
        terminal = Terminal.from_id(self.view.id())
        if not terminal or not terminal.alive:
            return
        if self.page_scrolled(terminal, **kwargs):
            return
        # drop any selection so the caret follows the terminal cursor
        self.view.sel().clear()
        self.view.run_command("vterm_show_cursor")
        terminal.send_key(**kwargs)

    def page_scrolled(self, terminal, key=None, **mods):
        """Page the view one screen over the scrollback, before vterm_show_cursor,
        whose scroll would pull the viewport straight back down. The alt screen is
        folded and has no scroll range, so an app there keeps the key.

        Returns:
            bool: the view consumed the key; False sends it to the app.
        """
        # the cheap tests first: this runs on every keystroke
        if key not in ("pageup", "pagedown") or not mods or terminal.screen.alternate_buffer_mode:
            return False
        modifier = settings().get("page_scroll_modifier", "shift")
        # exact match: ctrl+shift+pageup still belongs to the app
        if not modifier or {m for m, on in mods.items() if on} != set(modifier.split("+")):
            return False
        scroll_page(self.view, terminal, up=(key == "pageup"))
        return True


class VtermCopyCommand(sublime_plugin.TextCommand):
    """Copies the selection, re-joining rows the emulator soft-wrapped."""

    def run(self, edit):
        """Strip the soft-wrap sentinels, then copy as usual."""
        view = self.view
        if not view.settings().get("vterm_view"):
            return
        text = "\n".join(view.substr(s) for s in view.sel())

        # re-join soft-wrapped lines
        text = text.replace(CONTINUATION + "\n", "")
        text = text.replace(CONTINUATION, "")

        sublime.set_clipboard(text)


class VtermPasteCommand(sublime_plugin.TextCommand):
    """Pastes the clipboard into the app; replaces sublime's own paste."""

    def run(self, edit):
        """Hand the clipboard to vterm_paste_text."""
        self.view.run_command("vterm_paste_text", {"text": sublime.get_clipboard()})


class VtermPasteTextCommand(sublime_plugin.TextCommand):
    """Sends arbitrary text to the app, bracketed when it asked for that."""

    def run(self, edit, text, bracketed=True):
        """Args:
        bracketed: wrap in paste brackets, if the app enabled ?2004. IME
            text comes through unbracketed — it was typed, not pasted.
        """
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


class VtermSelectThemeCommand(sublime_plugin.WindowCommand):
    """Pick a colour palette, previewing each as it is highlighted"""

    def run(self):
        """Show the palette picker, starting on the configured theme."""
        self.settings = settings()
        self.original = self.settings.get("theme")
        self.choices = [None] + sorted(THEMES)

        items = [["No theme", "follow the Sublime color scheme"]]
        for name in self.choices[1:]:
            theme = THEMES[name]
            items.append([name, "{} on {}".format(theme["foreground"], theme["background"])])

        current = self.choices.index(self.original) if self.original in self.choices else 0
        self.window.show_quick_panel(items, self._done, selected_index=current, on_highlight=self._preview)

    def _preview(self, index):
        """Apply a palette while it is highlighted, without saving it."""
        self.settings.set("theme", self.choices[index])

    def _done(self, index):
        """Persist the choice, or restore the original if the panel was cancelled."""
        if index == -1:
            self.settings.set("theme", self.original)
            return
        self.settings.set("theme", self.choices[index])
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


class VtermCloseCommand(sublime_plugin.TextCommand):
    """Kills the process and closes the tab. Registered but bound nowhere; for user keybindings."""

    def run(self, _):
        """Kill the process, then close the view."""
        view = self.view
        if not view.settings().get("vterm_view"):
            return
        terminal = Terminal.from_id(view.id())
        if terminal:
            terminal.kill()
        view.close()


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
