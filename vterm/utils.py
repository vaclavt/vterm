""" Small helpers shared across vterm modules """
import time
from contextlib import contextmanager
from functools import wraps

import sublime
from pyte.screens import wcwidth

# the plugin's own settings file, loaded once by settings()
SETTINGS_FILE = "vterm.sublime-settings"

_settings = None


def settings():
    """Cached settings handle; a sublime Settings object stays live"""
    global _settings
    if _settings is None:
        _settings = sublime.load_settings(SETTINGS_FILE)
    return _settings


def responsive(period=0.1, default=True):
    """Rate-limit a function; return `default` between invocations."""

    def wrapper(f):
        # a list so the inner closure can mutate it without `nonlocal`
        t = [0]

        @wraps(f)
        def _(*args, **kwargs):
            now = time.time()
            if now - t[0] > period:
                t[0] = now
                return f(*args, **kwargs)
            else:
                return default

        return _

    return wrapper


@contextmanager
def intermission(period=0.1):
    """Sleep out the remainder of `period` seconds after the body ran."""
    startt = time.time()
    yield
    deltat = time.time() - startt
    if deltat < period:
        time.sleep(period - deltat)


def rev_wcwidth(text, width):
    """Index into `text` at which the substring reaches `width` cells.

    The inverse of wcswidth: maps a terminal column back onto a character index,
    so a wide char or a tab does not put the caret in the wrong place.
    """
    if width == 0:
        return -1

    w = 0
    i = -1
    for i, c in enumerate(text):
        w += wcwidth(c)
        if w >= width:
            break
    if w >= width:
        return i
    else:
        return i + width - w
