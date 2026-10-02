""" Theme picker flows and the selection-to-text helper.
Run with: python3 tests/test_commands.py """
import _bootstrap  # sets sys.path for the imports below
from _bootstrap import FakeProcess

import sublime
from vterm.commands import VtermPasteSelectionCommand, VtermSelectThemeCommand, selection_text
from vterm.emulator import VtScreen
from vterm.render import CONTINUATION
from vterm.scheme import get_scheme_manager
from vterm.terminal import Terminal
from vterm.utils import SETTINGS_FILE
from vterm.themes import THEMES

settings = sublime.load_settings(SETTINGS_FILE)
panel = {}


class FakeTerminalView:
    """Just enough view for a Terminal to be rethemed: an id and its own settings."""

    _next_id = 1

    def __init__(self):
        self._id = FakeTerminalView._next_id
        FakeTerminalView._next_id += 1
        self._settings = sublime.Settings()

    def id(self):
        return self._id

    def settings(self):
        return self._settings

    def is_valid(self):
        return True

    def style(self):
        return {}


class FakeWindow:
    def __init__(self, view):
        self._view = view

    def active_view(self):
        return self._view

    def show_quick_panel(self, items, on_done, selected_index=-1, on_highlight=None):
        panel.update(items=items, on_done=on_done,
                     selected_index=selected_index, on_highlight=on_highlight)


def fake_terminal(theme_name):
    """A terminal frozen to a theme, registered the way a real one is."""
    view = FakeTerminalView()
    terminal = Terminal(view, get_scheme_manager(theme_name))
    terminal.screen = VtScreen(20, 3, process=FakeProcess())
    Terminal._terminals[view.id()] = terminal
    return terminal


def open_picker(terminal):
    command = VtermSelectThemeCommand(FakeWindow(terminal.view))
    command.run()
    return command


# every theme is offered, with "no theme" first
command = open_picker(fake_terminal(None))
assert [i[0] for i in panel["items"]] == ["No theme"] + sorted(THEMES), panel["items"]
assert panel["selected_index"] == 0

# the focused terminal's own theme starts highlighted, not the global default
settings.set("theme", "homebrew")
terminal = fake_terminal("pro")
command = open_picker(terminal)
assert panel["selected_index"] == command.choices.index("pro")

# highlighting previews the focused terminal alone, leaving the global default and
# every other terminal untouched
other = fake_terminal("default")
saves = len(sublime._saved)
panel["on_highlight"](command.choices.index("slate"))
assert terminal.scheme.theme_name == "slate", terminal.scheme.theme_name
assert terminal.view.settings().get("color_scheme") == terminal.scheme.path
assert other.scheme.theme_name == "default", "another open terminal must not retheme"
assert settings.get("theme") == "homebrew", "preview must not touch the global default"
assert len(sublime._saved) == saves, "preview must not save settings"

# cancelling restores the focused terminal's theme from before the panel opened
panel["on_done"](-1)
assert terminal.scheme.theme_name == "pro", terminal.scheme.theme_name

# confirming retheme the focused terminal and persists the default for new terminals
command = open_picker(terminal)
panel["on_done"](command.choices.index("homebrew"))
assert terminal.scheme.theme_name == "homebrew", terminal.scheme.theme_name
assert settings.get("theme") == "homebrew", settings.get("theme")
assert sublime._saved[-1] == SETTINGS_FILE
assert other.scheme.theme_name == "default", "another open terminal must not retheme"

# "no theme" persists as null rather than a name
command = open_picker(fake_terminal("slate"))
panel["on_done"](0)
assert settings.get("theme") is None, settings.get("theme")

print("theme picker tests passed ({} themes offered)".format(len(THEMES)))


# a palette change must reach the screen it was applied to: it answers OSC 10/11
# and DSR 996 from these values
from vterm.emulator import VtStream

recorder = FakeProcess()
terminal = fake_terminal("default")
terminal.screen = VtScreen(20, 3, process=recorder, fg_color="000000",
                           bg_color="ffffff", is_dark=False)
terminal.process = recorder
stream = VtStream(terminal.screen)

terminal.scheme = get_scheme_manager("slate")
terminal.apply_scheme()
assert terminal.screen.default_fg_color == terminal.scheme.foreground.lstrip("#")
assert terminal.screen.default_bg_color == terminal.scheme.background.lstrip("#")

# without mode 2031 the app is not interrupted
assert recorder.written == "", repr(recorder.written)

# with it, the dark/light report is sent so the app can restyle itself
stream.feed("\x1b[?2031h")
recorder.written = ""
terminal.apply_scheme()
assert recorder.written == "\x1b[?997;1n", repr(recorder.written)

# and the emulator answers a colour query with the new palette
recorder.written = ""
stream.feed("\x1b]11;?\x07")
bg = terminal.screen.default_bg_color
expected = "\x1b]11;rgb:{0}{0}/{1}{1}/{2}{2}\x07".format(bg[0:2], bg[2:4], bg[4:6])
assert recorder.written == expected, repr(recorder.written)

print("live theme tests passed (running screen adopts palette, app notified)")


# the scheme file must exist before any restored tab's color_scheme is loaded at
# startup, and building a theme twice must not duplicate its seeded rules
import os

scheme = get_scheme_manager("pro")
path = os.path.join(scheme._directory(), scheme.file_name)
assert os.path.exists(path), "get_scheme_manager did not write the scheme"

# the cached instance is reused, not rebuilt
assert get_scheme_manager("pro") is scheme, "theme managers must be cached"

# build is idempotent
with scheme.lock:
    first = len(scheme.rules)
scheme.build()
assert len(scheme.rules) == first, \
    "rules duplicated: {} -> {}".format(first, len(scheme.rules))

print("scheme file tests passed ({} seeded rules, idempotent)".format(first))


class FakeView:
    """Just enough view for selection_text and the paste-selection command."""

    def __init__(self, text, regions):
        self.text = text
        self.regions = list(regions)
        self.commands = []

    def substr(self, region):
        return self.text[region.begin():region.end()]

    def sel(self):
        return self

    def __iter__(self):
        return iter(self.regions)

    def clear(self):
        self.regions = []

    def run_command(self, name, args=None):
        self.commands.append((name, args))


def paste(view):
    command = VtermPasteSelectionCommand.__new__(VtermPasteSelectionCommand)
    command.view = view
    command.run(None)
    return view.commands


# soft-wrapped rows re-join: the sentinel and the newline behind it both go
wrapped = "abc" + CONTINUATION + "\ndef"
view = FakeView(wrapped, [sublime.Region(0, len(wrapped))])
assert selection_text(view) == "abcdef", repr(selection_text(view))

# a trailing sentinel with no newline is still stripped
view = FakeView("abc" + CONTINUATION, [sublime.Region(0, 3 + len(CONTINUATION))])
assert selection_text(view) == "abc", repr(selection_text(view))

# multiple regions join with a newline
view = FakeView("abcdef", [sublime.Region(0, 3), sublime.Region(3, 6)])
assert selection_text(view) == "abc\ndef", repr(selection_text(view))

# the middle button pastes the selection and drops it, so the caret follows again
view = FakeView("abcdef", [sublime.Region(0, 3)])
assert paste(view) == [("vterm_paste_text", {"text": "abc"})], view.commands
assert list(view.sel()) == [], "selection must be cleared"

# nothing selected, nothing sent
view = FakeView("abcdef", [sublime.Region(2, 2)])
assert paste(view) == [], view.commands

print("selection tests passed (sentinel stripping, paste, empty no-op)")
