""" Smoke test: every plugin module imports cleanly against the sublime stubs.
Run with: python3 tests/test_imports.py """
import _bootstrap  # sets sys.path for the imports below

ROOT = _bootstrap.ROOT

import vterm.utils
import vterm.keys
import vterm.emulator
import vterm.scheme
import vterm.terminal
import vterm.render
import vterm.commands
import vterm.listeners
import vterm.mouse

# keymap and mousemap must parse the way sublime parses them: JSON with
# // line comments
import importlib
import json
import os
import re
import sys
import types


def load_sublime_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.loads(re.sub(r"^\s*//.*$", "", f.read(), flags=re.M))


keymap = load_sublime_json("Default.sublime-keymap")
assert len(keymap) > 100

# every printable must be bound, or sublime inserts it into the view and an app
# that does not echo (vim in normal mode) leaves the character behind. Each
# binding's key must also round-trip to that same character: the shift+<letter>
# form silently yields lowercase, which is why uppercase is bound literally.
import string  # noqa
from vterm.keys import get_key_code

bound = {}
for entry in keymap:
    combo = entry["keys"][0]
    if len(combo) == 1 and entry["command"] == "vterm_keypress":
        assert combo not in bound, "{} bound twice".format(combo)
        bound[combo] = entry["args"]

for char in string.ascii_letters + string.digits + string.punctuation:
    assert char in bound, "printable {!r} is not bound".format(char)
    produced = get_key_code(**bound[char])
    assert produced == char, "{!r} would send {!r}".format(char, produced)

assert get_key_code(key="space") == " ", "space must send a space"

# legacy encodings by default; while the app has modifyOtherKeys on, the keys
# whose modified form is ambiguous take the CSI 27 form instead. a bare ESC CR
# reads as alt+enter
assert get_key_code(key="enter", shift=True) == "\x1b\r"
assert get_key_code(key="enter", shift=True, modify_other_keys=2) == "\x1b[27;2;13~"
assert get_key_code(key="space", ctrl=True, modify_other_keys=2) == "\x1b[27;5;32~"
assert get_key_code(key="enter", modify_other_keys=2) == "\r", "unmodified keys stay legacy"
assert get_key_code(key="c", ctrl=True, modify_other_keys=2) == "\x03", "ctrl+c stays a control byte"
load_sublime_json("Default (OSX).sublime-mousemap")

# a theme's scheme manager produces scopes and rules
from vterm.scheme import get_scheme_manager
scope = get_scheme_manager("default").scope("ff0000", "default", "b")
assert scope == "vterm.ff0000.default.b", scope

# every command referenced by the palette and the keymap must exist, and the
# flags commands set must be the ones the renderer reads (a rename here is
# otherwise only caught at runtime)
import vterm.commands as commands
import vterm.render as render
import vterm.terminal as terminal_module
from vterm.terminal import Terminal


def command_class_name(name):
    return "".join(part.title() for part in name.split("_")) + "Command"


for entry in load_sublime_json("vterm.sublime-commands") + keymap:
    cls = command_class_name(entry["command"])
    assert hasattr(commands, cls) or hasattr(render, cls), \
        "{} -> missing {}".format(entry["command"], cls)

# every pending_* name mentioned in the modules must be a real Terminal
# attribute, so a renamed flag cannot leave a writer pointing at nothing
flags = {f for f in vars(Terminal(None, None)) if f.startswith("pending_")}
assert flags == {"pending_clear", "pending_clear_screen", "pending_render"}, flags
for module in (commands, render, terminal_module):
    with open(module.__file__, encoding="utf-8") as f:
        # (?<!_) skips the private _pending_to_send_string write-queue flag
        for name in set(re.findall(r"(?<!_)pending_[a-z_]+", f.read())):
            assert name in flags, "{}: {} is not a Terminal attribute".format(
                os.path.basename(module.__file__), name)

# a theme is data, so a typo'd colour name would only show up as a silently
# unstyled scope at runtime
from vterm.emulator import ANSI_COLORS
from vterm.themes import THEMES

for name, theme in THEMES.items():
    missing = (set(ANSI_COLORS) | {"foreground", "background"}) - set(theme)
    assert not missing, "theme {}: missing {}".format(name, sorted(missing))
    assert not set(theme) - (set(ANSI_COLORS) | {"foreground", "background"}), name
    for key, value in theme.items():
        assert re.fullmatch(r"#[0-9a-f]{6}", value), \
            "theme {}: {} = {!r}".format(name, key, value)

configured = load_sublime_json("vterm.sublime-settings").get("theme")
assert configured is None or configured in THEMES, configured

# plugin_loaded must create every theme's colour scheme: sublime loads a restored
# tab's color_scheme during startup, before any terminal opens, and it may name
# any of them
# import main.py the way sublime does - as a module of the package directory -
# so its relative imports and plugin_loaded body are actually executed
from vterm.scheme import SchemeManager
from vterm.themes import ALL_THEME_NAMES

scheme_files = [SchemeManager(name).file_path() for name in ALL_THEME_NAMES]
for scheme_file in scheme_files:
    if os.path.exists(scheme_file):
        os.remove(scheme_file)

pkg = types.ModuleType("vterm")
pkg.__path__ = [ROOT]
sys.modules["vterm"] = pkg
main = importlib.import_module("vterm.main")

main.plugin_loaded()
for name, scheme_file in zip(ALL_THEME_NAMES, scheme_files):
    assert os.path.exists(scheme_file), \
        "plugin_loaded must leave {}'s colour scheme on disk".format(name)

print("import smoke test passed ({} keymap bindings, {} pending flags, "
      "{} themes)".format(len(keymap), len(flags), len(THEMES)))
