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
load_sublime_json("Default (OSX).sublime-mousemap")

# scheme manager produces scopes and rules
import vterm.scheme as scheme_module
from vterm.scheme import scheme_manager
scope = scheme_manager.scope("ff0000", "default", "b")
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
flags = {f for f in vars(Terminal(None)) if f.startswith("pending_")}
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

# plugin_loaded must create the colour scheme: sublime loads a restored tab's
# color_scheme during startup, before any terminal opens
# import main.py the way sublime does - as a module of the package directory -
# so its relative imports and plugin_loaded body are actually executed
pkg = types.ModuleType("vterm")
pkg.__path__ = [ROOT]
sys.modules["vterm"] = pkg
main = importlib.import_module("vterm.main")

scheme_file = os.path.join(main.scheme_manager._directory(), scheme_module.SCHEME_FILE_NAME)
if os.path.exists(scheme_file):
    os.remove(scheme_file)
main.plugin_loaded()
assert os.path.exists(scheme_file), \
    "plugin_loaded must leave the colour scheme on disk"

print("import smoke test passed ({} keymap bindings, {} pending flags, "
      "{} themes)".format(len(keymap), len(flags), len(THEMES)))
