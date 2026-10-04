""" Colour scheme generated on demand: a region can only name a scope, and a scope's colours must already exist in the scheme """
import json
import logging
import os
import threading

import sublime

from .emulator import ANSI_COLORS
from .utils import settings
from .themes import ALL_THEME_NAMES, THEMES

LOG = logging.getLogger(__name__)

# written under Packages/User so it survives a package reinstall
SCHEME_DIR_NAME = "User/vterm"

# each write makes sublime reload the scheme and restyle every view
WRITE_DEBOUNCE_MS = 250

# palette used when no theme is selected, overridable via vterm.sublime-settings
DEFAULT_ANSI = {
    "black": "#000000",
    "red": "#cd3131",
    "green": "#0dbc79",
    "brown": "#e5e510",
    "blue": "#2472c8",
    "magenta": "#bc3fbc",
    "cyan": "#11a8cd",
    "white": "#e5e5e5",
    "light_black": "#666666",
    "light_red": "#f14c4c",
    "light_green": "#23d18b",
    "light_brown": "#f5f543",
    "light_blue": "#3b8eea",
    "light_magenta": "#d670d6",
    "light_cyan": "#29b8db",
    "light_white": "#ffffff",
}


def next_color(color_text):
    """Nudge a #rrggbb colour by one; add_regions inverts fg/bg when they match exactly"""
    hex_value = int(color_text[5:7], 16)
    if hex_value == 255:
        return color_text[:5] + "fe"
    else:
        return color_text[:5] + "{:02x}".format(hex_value + 1)


def blend(base, other, ratio):
    """Mix two #rrggbb colours"""
    channels = []
    for i in (1, 3, 5):
        a = int(base[i : i + 2], 16)
        b = int(other[i : i + 2], 16)
        channels.append(round(a + (b - a) * ratio))
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _is_dark(color_text):
    """True when a #rrggbb colour is dark by perceived luminance."""
    r, g, b = (int(color_text[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b < 128


class SchemeManager:
    """Owns one theme's generated scheme file and set of known scopes."""

    def __init__(self, theme_name):
        """State only; build() resolves the palette and writes the file."""
        self.theme_name = theme_name
        self.file_name = "vterm-{}.hidden-color-scheme".format(theme_name or "system")
        self.path = "Packages/{}/{}".format(SCHEME_DIR_NAME, self.file_name)
        self.lock = threading.Lock()
        self.rules = {}
        # key -> scope name; the render thread reads it without the lock, so it
        # must never outlive self.rules (cleared together in build)
        self._names = {}
        self.foreground = "#ffffff"
        self.background = "#000000"
        self.ansi = dict(DEFAULT_ANSI)
        self._write_pending = False
        self._dir = None

    def build(self, host_view=None):
        """Resolve the palette and regenerate every rule, then write the file.

        Rules cannot be appended after a palette change: each one stores resolved
        colours, so they all have to be rebuilt. Callable again later (e.g. to
        re-sample the "No theme" palette from the editor's current colours).
        """
        with self.lock:
            self._names = {}
            self.rules = {}
            self.ansi = dict(DEFAULT_ANSI)

            theme = THEMES.get(self.theme_name)

            if theme:
                self.foreground = theme["foreground"]
                self.background = theme["background"]
                self.ansi.update({name: theme[name] for name in ANSI_COLORS if name in theme})
            elif host_view is not None and host_view.is_valid():
                style = host_view.style()
                self.foreground = style.get("foreground", self.foreground)
                self.background = style.get("background", self.background)

            # user overrides sit on top of whichever palette was chosen
            user_ansi = settings().get("ansi_colors", {})
            for name in ANSI_COLORS:
                if name in user_ansi:
                    self.ansi[name] = user_ansi[name]

            # seeded so the first frame does not reference scopes the file lacks
            for fg in ANSI_COLORS:
                self._add_rule(fg, "default", "")
                self._add_rule(fg, "default", "b")
            self._add_rule("default", "default", "b")
            self._add_rule("reverse_default", "reverse_default", "")

            self._write_file()

    def is_dark(self):
        """Whether the active palette is dark; reported to the app via OSC 11 and DSR 996."""
        return _is_dark(self.background)

    def scope(self, fg, bg, style):
        """Scope for a colour combination, creating its rule if new. Thread-safe"""
        key = (fg, bg, style)
        # hot path: one GIL-atomic dict read, no lock, no formatting
        name = self._names.get(key)
        if name is not None:
            return name
        with self.lock:
            if key not in self.rules:
                self._add_rule(fg, bg, style)
                if not self._write_pending:
                    self._write_pending = True
                    # a warm-up burst coalesces into one write, off the UI thread
                    sublime.set_timeout_async(self._flush, WRITE_DEBOUNCE_MS)
            name = self._scope_name(fg, bg, style)
            self._names[key] = name
        return name

    def _scope_name(self, fg, bg, style):
        """Scope for a colour combination, e.g. vterm.red.default.b"""
        if style:
            return "vterm.{}.{}.{}".format(fg, bg, style)
        return "vterm.{}.{}".format(fg, bg)

    def _resolve(self, color, for_bg):
        """Turn a pyte colour name into #rrggbb against the active palette."""
        if color == "default":
            return self.background if for_bg else self.foreground
        if color == "reverse_default":
            return self.foreground if for_bg else self.background
        if color in self.ansi:
            return self.ansi[color]
        return "#" + color

    def _add_rule(self, fg, bg, style):
        """Add the scheme rule for one colour combination. Caller holds the lock."""
        fg_color = self._resolve(fg, for_bg=False)
        bg_color = self._resolve(bg, for_bg=True)
        if bg_color.lower() == self.background.lower():
            bg_color = next_color(bg_color)
        rule = {
            "scope": self._scope_name(fg, bg, style),
            "foreground": fg_color,
            "background": bg_color,
        }
        font_style = []
        if "b" in style:
            font_style.append("bold")
        if "i" in style:
            font_style.append("italic")
        if "u" in style:
            font_style.append("underline")
        if font_style:
            rule["font_style"] = " ".join(font_style)
        self.rules[(fg, bg, style)] = rule

    def file_path(self):
        """Filesystem path of the generated scheme file."""
        return os.path.join(self._directory(), self.file_name)

    def _directory(self):
        """Path of the generated scheme's directory, created on first use."""
        if self._dir is None:
            self._dir = os.path.join(sublime.packages_path(), *SCHEME_DIR_NAME.split("/"))
            os.makedirs(self._dir, exist_ok=True)
        return self._dir

    def _flush(self):
        """Debounced write, on sublime's async thread."""
        with self.lock:
            self._write_pending = False
            self._write_file()

    def _write_file(self):
        """Serialize globals and every rule to the scheme file. Caller holds the lock."""
        scheme = {
            "name": "vterm",
            "globals": {
                "background": self.background,
                "foreground": self.foreground,
                "caret": self.foreground,
                "block_caret": self.foreground,
                "gutter": self.background,
                "selection": blend(self.background, self.foreground, 0.3),
                "selection_foreground": self.foreground,
                "selection_border_width": "0",
                "inactive_selection": blend(self.background, self.foreground, 0.15),
            },
            "rules": list(self.rules.values()),
        }
        with open(self.file_path(), "w", encoding="utf-8") as f:
            # compact: sublime reparses the whole file on every write
            json.dump(scheme, f, separators=(",", ":"))
        LOG.debug("scheme written: rules=%s", len(self.rules))


# one manager per theme name, built once and kept for the plugin host's life: the
# built-in theme set is small and fixed, so there is nothing to garbage-collect
_managers = {}


def get_scheme_manager(theme_name, host_view=None):
    """The manager for a theme name (None for "follow the Sublime colour scheme"),
    building and caching it on first use.

    The None entry is rebuilt whenever the editor's colours have changed since: it
    is a live sample of them, not a static palette, so every caller handing a
    terminal a "no theme" scheme gets one that is actually current.
    """
    manager = _managers.get(theme_name)
    if manager is None:
        manager = SchemeManager(theme_name)
        _managers[theme_name] = manager
        manager.build(host_view)
    elif theme_name is None and host_view is not None and host_view.is_valid():
        style = host_view.style()
        if (style.get("foreground"), style.get("background")) != (manager.foreground, manager.background):
            manager.build(host_view)
    return manager


def build_startup_schemes(host_view=None):
    """Make sure every theme's colour scheme is on disk before Sublime reads a
    restored tab's color_scheme at startup.

    A named theme is static, so an existing file is left alone - rebuilding it
    would just rewrite the same bytes, and each write makes sublime reload the
    scheme and restyle every view using it. None always rebuilds, same as
    get_scheme_manager.
    """
    for name in ALL_THEME_NAMES:
        if name is None:
            get_scheme_manager(None, host_view=host_view)
            continue
        if not os.path.exists(SchemeManager(name).file_path()):
            get_scheme_manager(name)
