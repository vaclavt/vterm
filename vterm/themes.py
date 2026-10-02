""" Terminal colour palettes

The macOS Terminal.app classics, in the colour naming scheme.py already uses
(the names come from pyte). Selected with the `theme` setting; without one, the
terminal follows the active Sublime colour scheme instead.
"""

# the 16 ANSI colours every Terminal.app theme below shares; only fg/bg differ
_TERMINAL_APP_ANSI = {
    "black": "#000000",
    "red": "#990000",
    "green": "#00a600",
    "brown": "#999900",
    "blue": "#0000b2",
    "magenta": "#b200b2",
    "cyan": "#00a6b2",
    "white": "#bfbfbf",
    "light_black": "#666666",
    "light_red": "#e50000",
    "light_green": "#00d900",
    "light_brown": "#e5e500",
    "light_blue": "#0000ff",
    "light_magenta": "#e500e5",
    "light_cyan": "#00e5e5",
    "light_white": "#e5e5e5",
}

# selectable palettes, by the name the "theme" setting takes
THEMES = {
    "default": dict(_TERMINAL_APP_ANSI, foreground="#000000", background="#ffffff"),
    "homebrew": dict(_TERMINAL_APP_ANSI, foreground="#00ff00", background="#000000"),
    "pro": dict(_TERMINAL_APP_ANSI, foreground="#f2f2f2", background="#000000", blue="#2009db"),
    "slate": {
        "foreground": "#35b1d2",
        "background": "#222222",
        "black": "#222222",
        "red": "#e2a8bf",
        "green": "#81d778",
        "brown": "#c4c9c0",
        "blue": "#264b49",
        "magenta": "#a481d3",
        "cyan": "#15ab9c",
        "white": "#02c5e0",
        "light_black": "#ffffff",
        "light_red": "#ffcdd9",
        "light_green": "#beffa8",
        "light_brown": "#d0ccca",
        "light_blue": "#7ab0d2",
        "light_magenta": "#c5a7d9",
        "light_cyan": "#8cdfe0",
        "light_white": "#e0e0e0",
    },
    "red-alert": {
        "foreground": "#ffffff",
        "background": "#762423",
        "black": "#000000",
        "red": "#d62e4e",
        "green": "#71be6b",
        "brown": "#beb86b",
        "blue": "#489bee",
        "magenta": "#e979d7",
        "cyan": "#6bbeb8",
        "white": "#d6d6d6",
        "light_black": "#262626",
        "light_red": "#e02553",
        "light_green": "#aff08c",
        "light_brown": "#dfddb7",
        "light_blue": "#65aaf1",
        "light_magenta": "#ddb7df",
        "light_cyan": "#b7dfdd",
        "light_white": "#ffffff",
    },
}

# every selectable theme, including None for "follow the active Sublime colour scheme"
ALL_THEME_NAMES = [None] + sorted(THEMES)
