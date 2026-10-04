""" vterm plugin entry point

Submodules are deliberately not purged on reload: rebuilding the terminal
registry would orphan open terminal tabs.
"""
import logging

import sublime

# imported for sublime's command discovery, not called here
from .vterm.commands import (
    VtermClearScreenCommand,
    VtermClearUndoStackCommand,
    VtermCleanupCommand,
    VtermCloseCommand,
    VtermCopyCommand,
    VtermInitializeViewCommand,
    VtermKeypressCommand,
    VtermNoopCommand,
    VtermOpenCommand,
    VtermOpenTerminalProjectFolderCommand,
    VtermPasteCommand,
    VtermPasteSelectionCommand,
    VtermPasteTextCommand,
    VtermSelectThemeCommand,
)
from .vterm.listeners import VtermCoreEventListener
from .vterm.mouse import (
    VtermMouseClickCommand,
    VtermMouseEventListener,
    VtermScrollCommand,
)
from .vterm.scheme import build_startup_schemes
from .vterm.utils import settings
from .vterm.render import (
    VtermRenderCommand,
    VtermShowCursorCommand,
)


def plugin_loaded():
    """Write any missing theme's colour scheme and set the log level, once per
    plugin host: a restored tab's color_scheme is read during startup and may
    name any of them.
    """
    window = sublime.active_window()
    view = window.active_view() if window else None
    build_startup_schemes(view)

    level = logging.DEBUG if settings().get("debug", False) else logging.INFO
    logging.basicConfig()
    logging.getLogger("vterm").setLevel(level)
