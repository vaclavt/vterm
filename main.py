""" vterm plugin entry point

Submodules are deliberately not purged on reload: rebuilding the terminal
registry would orphan open terminal tabs.
"""
import logging

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
    VtermPasteTextCommand,
    VtermSelectThemeCommand,
)
from .vterm.listeners import VtermCoreEventListener
from .vterm.mouse import (
    VtermMouseClickCommand,
    VtermMouseEventListener,
    VtermScrollCommand,
)
from .vterm.scheme import scheme_manager
from .vterm.utils import settings
from .vterm.render import (
    VtermRenderCommand,
    VtermShowCursorCommand,
)


def plugin_loaded():
    """Write the colour scheme and set the log level, once per plugin host."""
    # synchronous: a restored tab's color_scheme is read during startup
    scheme_manager.ensure_file()

    level = logging.DEBUG if settings().get("debug", False) else logging.INFO
    logging.basicConfig()
    logging.getLogger("vterm").setLevel(level)
