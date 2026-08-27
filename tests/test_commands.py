""" Theme picker flows: preview, cancel, confirm.
Run with: python3 tests/test_commands.py """
import _bootstrap  # sets sys.path for the imports below
from _bootstrap import FakeProcess

import sublime
from vterm.commands import VtermSelectThemeCommand
from vterm.utils import SETTINGS_FILE
from vterm.themes import THEMES

settings = sublime.load_settings(SETTINGS_FILE)
panel = {}


class FakeWindow:
    def show_quick_panel(self, items, on_done, selected_index=-1, on_highlight=None):
        panel.update(items=items, on_done=on_done,
                     selected_index=selected_index, on_highlight=on_highlight)


def open_picker(current):
    settings.set("theme", current)
    command = VtermSelectThemeCommand(FakeWindow())
    command.run()
    return command


# every theme is offered, with "no theme" first
command = open_picker(None)
assert [i[0] for i in panel["items"]] == ["No theme"] + sorted(THEMES), panel["items"]
assert panel["selected_index"] == 0

# the configured theme starts highlighted
command = open_picker("pro")
assert panel["selected_index"] == command.choices.index("pro")

# highlighting previews but must not persist
saves = len(sublime._saved)
panel["on_highlight"](command.choices.index("slate"))
assert settings.get("theme") == "slate", settings.get("theme")
assert len(sublime._saved) == saves, "preview must not save settings"

# cancelling restores the value from before the panel opened
panel["on_done"](-1)
assert settings.get("theme") == "pro", settings.get("theme")

# confirming persists
command = open_picker("pro")
panel["on_done"](command.choices.index("homebrew"))
assert settings.get("theme") == "homebrew", settings.get("theme")
assert sublime._saved[-1] == SETTINGS_FILE

# "no theme" persists as null rather than a name
command = open_picker("slate")
panel["on_done"](0)
assert settings.get("theme") is None, settings.get("theme")

print("theme picker tests passed ({} themes offered)".format(len(THEMES)))


# a palette change must reach terminals that are already running: the screen
# answers OSC 10/11 and DSR 996 from these values
from vterm.emulator import VtScreen, VtStream
from vterm.scheme import scheme_manager
from vterm.terminal import Terminal


recorder = FakeProcess()
# real __init__ (view unused here); process/screen stand in for start()
terminal = Terminal(None)
terminal.screen = VtScreen(20, 3, process=recorder, fg_color="000000",
                           bg_color="ffffff", is_dark=False)
terminal.process = recorder
stream = VtStream(terminal.screen)

scheme_manager.foreground = "#eeeeee"
scheme_manager.background = "#111111"
terminal.apply_scheme()
assert terminal.screen.default_bg_color == "111111", terminal.screen.default_bg_color
assert terminal.screen.theme_is_dark is True

# without mode 2031 the app is not interrupted
assert recorder.written == "", repr(recorder.written)

# with it, the dark/light report is sent so the agent can restyle itself
stream.feed("\x1b[?2031h")
recorder.written = ""
terminal.apply_scheme()
assert recorder.written == "\x1b[?997;1n", repr(recorder.written)

# and the emulator answers a colour query with the new palette
recorder.written = ""
stream.feed("\x1b]11;?\x07")
assert recorder.written == "\x1b]11;rgb:1111/1111/1111\x07", repr(recorder.written)

print("live theme tests passed (running screen adopts palette, app notified)")


# the scheme file must exist before any restored tab's color_scheme is loaded at
# startup, and building twice must not duplicate the seeded rules
import os
from vterm.scheme import SCHEME_FILE_NAME

path = os.path.join(scheme_manager._directory(), SCHEME_FILE_NAME)
if os.path.exists(path):
    os.remove(path)

scheme_manager.ensure_file()
assert os.path.exists(path), "ensure_file did not write the scheme"
assert not scheme_manager._initialized, \
    "ensure_file must leave the palette open to refinement by initialize()"

# a second call is a no-op: same file, untouched
stamp = os.path.getmtime(path)
scheme_manager.ensure_file()
assert os.path.getmtime(path) == stamp, "ensure_file rewrote an existing scheme"

# _build is idempotent, which is what makes the ensure_file + initialize pair safe
with scheme_manager.lock:
    scheme_manager._build()
    first = len(scheme_manager.rules)
    scheme_manager._build()
assert len(scheme_manager.rules) == first, \
    "rules duplicated: {} -> {}".format(first, len(scheme_manager.rules))

print("scheme file tests passed ({} seeded rules, idempotent)".format(first))
