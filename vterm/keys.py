""" Key-name to escape-sequence tables

The keymap binds key *names*; get_key_code turns a name plus its modifier flags
into the bytes an app expects. Legacy xterm encodings only — the kitty keyboard
protocol is deliberately not implemented.
"""

# unmodified keys, and the paste brackets, in their default (non-application) form
_KEY_MAP = {
    "enter": "\r",
    "backspace": "\x7f",
    "tab": "\t",
    "space": " ",
    "escape": "\x1b",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "delete": "\x1b[3~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
    "bracketed_paste_start": "\x1b[200~",
    "bracketed_paste_end": "\x1b[201~",
}

# DECCKM: cursor keys switch from CSI to SS3 while an app asks for application mode
_APP_MODE_KEY_MAP = {
    "up": "\x1bOA",
    "down": "\x1bOB",
    "right": "\x1bOC",
    "left": "\x1bOD",
    "home": "\x1bOH",
    "end": "\x1bOF",
}

# xterm modified keys: CSI 1;<mod><letter> / CSI <n>;<mod>~
_MODIFIABLE_CSI = {
    "up": "\x1b[1;{}A",
    "down": "\x1b[1;{}B",
    "right": "\x1b[1;{}C",
    "left": "\x1b[1;{}D",
    "home": "\x1b[1;{}H",
    "end": "\x1b[1;{}F",
    "delete": "\x1b[3;{}~",
    "pageup": "\x1b[5;{}~",
    "pagedown": "\x1b[6;{}~",
}

# ctrl+<key> for the keys whose control byte is not chr(letter - 'a' + 1)
_CTRL_KEY_MAP = {
    "@": "\x00",
    "`": "\x00",
    "2": "\x00",
    "[": "\x1b",
    "{": "\x1b",
    "3": "\x1b",
    "\\": "\x1c",
    "|": "\x1c",
    "4": "\x1c",
    "]": "\x1d",
    "}": "\x1d",
    "5": "\x1d",
    "^": "\x1e",
    "~": "\x1e",
    "6": "\x1e",
    "_": "\x1f",
    "/": "\x1f",
    "7": "\x1f",
    "?": "\x7f",
    "8": "\x7f",
    "space": "\x00",
    "enter": "\n",
    "tab": "\t",
    "backspace": "\x08",
}

# the two shifted keys with an encoding of their own; shift is bare text elsewhere
_SHIFT_KEY_MAP = {
    "tab": "\x1b[Z",
    "enter": "\x1b\r",
}


def _modifier_code(ctrl, alt, shift):
    """xterm modifier parameter: 1 plus a bitmask of shift/alt/ctrl."""
    return 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)


def get_key_code(key, ctrl=False, alt=False, shift=False, application_mode=False, new_line_mode=False):
    """Escape sequence for a key name and its modifiers.

    Args:
        key: key name from the keymap, or a single literal character.
        application_mode: DECCKM is set, so cursor keys take their SS3 form.
        new_line_mode: LNM is set, so enter sends CR LF.

    Returns:
        str: the bytes to write to the pty.
    """
    key_lo = key.lower() if len(key) > 1 else key

    if (ctrl or alt or shift) and key_lo in _MODIFIABLE_CSI:
        return _MODIFIABLE_CSI[key_lo].format(_modifier_code(ctrl, alt, shift))

    if ctrl:
        base = key_lo
        if base in _CTRL_KEY_MAP:
            code = _CTRL_KEY_MAP[base]
        elif len(base) == 1 and "a" <= base <= "z":
            code = chr(ord(base) - ord("a") + 1)
        else:
            code = _plain(key, application_mode, new_line_mode)
        return ("\x1b" + code) if alt else code

    if alt:
        return "\x1b" + _plain(key if len(key) == 1 else key_lo, application_mode, new_line_mode)

    if shift:
        if key_lo in _SHIFT_KEY_MAP:
            return _SHIFT_KEY_MAP[key_lo]
        return _KEY_MAP.get(key, key)

    return _plain(key, application_mode, new_line_mode)


def _plain(key, application_mode=False, new_line_mode=False):
    """Unmodified encoding, honouring DECCKM and LNM; unknown names pass through as text."""
    if application_mode and key in _APP_MODE_KEY_MAP:
        return _APP_MODE_KEY_MAP[key]
    if new_line_mode and key == "enter":
        return "\r\n"
    if key in _KEY_MAP:
        return _KEY_MAP[key]
    return key
