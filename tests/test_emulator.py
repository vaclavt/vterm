""" Offline emulator tests: queries answered, modes tracked, parsing survives
chunk boundaries. Run with: python3 tests/test_emulator.py """
import _bootstrap  # sets sys.path for the imports below
from _bootstrap import FakeProcess

from vterm.emulator import VtScreen, VtStream


def make(columns=80, lines=24):
    proc = FakeProcess()
    screen = VtScreen(columns, lines, process=proc)
    stream = VtStream(screen)
    return proc, screen, stream


def feed(stream, data, char_by_char=False):
    if char_by_char:
        for c in data:
            stream.feed(c)
    else:
        stream.feed(data)


def run_case(char_by_char):
    tag = "char-by-char" if char_by_char else "chunked"

    # DA1 answered; apps can wait on it with no timeout
    proc, screen, stream = make()
    feed(stream, "\x1b[c", char_by_char)
    assert proc.written == "\x1b[?62;22c", (tag, proc.written)

    # CPR, plain and private form
    proc, screen, stream = make()
    feed(stream, "\x1b[5;10H\x1b[6n\x1b[?6n", char_by_char)
    assert proc.written == "\x1b[5;10R\x1b[?5;10R", (tag, proc.written)

    # XTVERSION then DECRQM 2026, the sync-output probe sequence
    proc, screen, stream = make()
    feed(stream, "\x1b[>0q\x1b[c\x1b[?2026$p\x1b[c", char_by_char)
    assert proc.written == (
        "\x1bP>|vterm 0.1\x1b\\" "\x1b[?62;22c"
        "\x1b[?2026;2$y" "\x1b[?62;22c"), (tag, proc.written)

    # DECRQM for DECAWM (7) and DECTCEM (25): both default-on, and the
    # wire-number list must answer them as set, not as unrecognized
    proc, screen, stream = make()
    feed(stream, "\x1b[?7$p\x1b[?25$p\x1b[?2026$p", char_by_char)
    assert proc.written == "\x1b[?7;1$y\x1b[?25;1$y\x1b[?2026;2$y", (tag, proc.written)

    # kitty probe: push ignored, query answered 0, DA1 fence answered
    proc, screen, stream = make()
    feed(stream, "\x1b[>7u\x1b[?u\x1b[c\x1b[<u", char_by_char)
    assert proc.written == "\x1b[?0u\x1b[?62;22c", (tag, proc.written)
    # and nothing leaked to the screen
    assert screen.buffer[0][0].data == " ", (tag, screen.buffer[0][0])

    # modifyOtherKeys: the level is tracked, and the sequence must not corrupt
    # SGR state
    proc, screen, stream = make()
    assert screen.modify_other_keys == 0, tag
    feed(stream, "\x1b[>4;2mhello", char_by_char)
    assert screen.modify_other_keys == 2, (tag, screen.modify_other_keys)
    assert screen.buffer[0][0].data == "h" and not screen.buffer[0][0].underscore, tag
    feed(stream, "\x1b[>4;0m", char_by_char)
    assert screen.modify_other_keys == 0, (tag, screen.modify_other_keys)
    # another resource, and the bare form, leave it alone
    feed(stream, "\x1b[>4;2m\x1b[>1;2m\x1b[>m", char_by_char)
    assert screen.modify_other_keys == 2, (tag, screen.modify_other_keys)

    # OSC 11 color query answered; OSC 0 sets title; OSC 133 dropped
    proc, screen, stream = make()
    screen.default_bg_color = "1a2b3c"
    feed(stream, "\x1b]0;mytitle\x07\x1b]11;?\x07\x1b]133;A\x07x", char_by_char)
    assert screen.title == "mytitle", (tag, screen.title)
    assert proc.written == "\x1b]11;rgb:1a1a/2b2b/3c3c\x07", (tag, proc.written)
    assert screen.buffer[0][0].data == "x", tag

    # OSC with ST terminator; a query asked with ST is answered with ST (meli)
    proc, screen, stream = make()
    screen.default_bg_color = "1a2b3c"
    feed(stream, "\x1b]2;st-title\x1b\\\x1b]11;?\x1b\\y", char_by_char)
    assert screen.title == "st-title", (tag, screen.title)
    assert proc.written == "\x1b]11;rgb:1a1a/2b2b/3c3c\x1b\\", (tag, proc.written)
    assert screen.buffer[0][0].data == "y", tag

    # OSC 52 clipboard (base64 "hi")
    got = []
    proc = FakeProcess()
    screen = VtScreen(80, 24, process=proc, clipboard_callback=got.append)
    stream = VtStream(screen)
    feed(stream, "\x1b]52;c;aGk=\x07", char_by_char)
    assert got == ["hi"], (tag, got)

    # bracketed paste and sync modes tracked
    proc, screen, stream = make()
    feed(stream, "\x1b[?2004h\x1b[?2026h", char_by_char)
    assert screen.bracketed_paste_enabled(), tag
    assert screen.in_sync_frame(), tag
    feed(stream, "\x1b[?2026l\x1b[?2004l", char_by_char)
    assert not screen.in_sync_frame(), tag
    assert not screen.bracketed_paste_enabled(), tag

    # mouse modes
    proc, screen, stream = make()
    feed(stream, "\x1b[?1000h\x1b[?1002h\x1b[?1004h\x1b[?1006h", char_by_char)
    assert screen.mouse_reporting_enabled(), tag
    assert screen.focus_tracking_enabled(), tag

    # alt screen switch and restore
    proc, screen, stream = make()
    feed(stream, "primary\x1b[?1049h\x1b[2Jalt\x1b[?1049l", char_by_char)
    row0 = "".join(screen.buffer[0][i].data for i in range(7))
    assert row0 == "primary", (tag, row0)
    assert not screen.alternate_buffer_mode, tag

    # truecolor SGR is quantized into the 256 palette; 256-color as hex; bold tracked
    proc, screen, stream = make()
    feed(stream, "\x1b[1;38;2;255;0;10mX\x1b[0m\x1b[38;5;196mY", char_by_char)
    assert screen.buffer[0][0].fg == "ff0000" and screen.buffer[0][0].bold, tag
    assert screen.buffer[0][1].fg == "ff0000", (tag, screen.buffer[0][1].fg)

    # SGR 5/25 (blink): ignored, not a crash — Char has no blink field
    proc, screen, stream = make()
    feed(stream, "\x1b[5;31mX\x1b[25mY", char_by_char)
    assert screen.buffer[0][0].fg == "red", (tag, screen.buffer[0][0])
    assert screen.buffer[0][1].fg == "red" and screen.buffer[0][1].data == "Y", tag

    # DECSTBM + SU/SD don't crash and scroll within margins
    proc, screen, stream = make(10, 6)
    feed(stream, "\x1b[2;5r\x1b[2S\x1b[1T\x1b[r", char_by_char)
    assert screen.margins is None, tag

    # DECSCUSR ignored, CSI t ignored, ?996n answered
    proc, screen, stream = make()
    feed(stream, "\x1b[2 q\x1b[16t\x1b[?996n", char_by_char)
    assert proc.written == "\x1b[?997;1n", (tag, proc.written)

    # scrollback: lines pushed to history when scrolling at bottom
    proc, screen, stream = make(10, 3)
    feed(stream, "a\r\nb\r\nc\r\nd", char_by_char)
    assert len(screen.history) == 1, (tag, len(screen.history))
    assert screen.history[0][0].data == "a", tag

    # ED 3 clears history and fires callback
    cleared = []
    proc = FakeProcess()
    screen = VtScreen(10, 3, process=proc, clear_callback=lambda: cleared.append(1))
    stream = VtStream(screen)
    feed(stream, "a\r\nb\r\nc\r\nd\x1b[3J", char_by_char)
    assert len(screen.history) == 0 and cleared, tag

    # DCS/APC payloads are consumed, not drawn
    proc, screen, stream = make()
    feed(stream, "\x1bP1$rpayload\x1b\\Z\x1b_Gapc\x1b\\W", char_by_char)
    row0 = "".join(screen.buffer[0][i].data for i in range(2))
    assert row0 == "ZW", (tag, row0)

    # DEC special graphics: xterm-256color's smacs is ESC(0, so ncurses apps
    # (mutt, dialog, less) draw borders with it when the locale is not UTF-8
    proc, screen, stream = make(20, 3)
    feed(stream, "\x1b(0lqqqk\x1b(Bplain", char_by_char)
    row = "".join(screen.buffer[0][x].data for x in range(10))
    assert row == "\u250c\u2500\u2500\u2500\u2510plain", (tag, repr(row))

    # G1 designation plus SO/SI stays ignored in UTF-8 mode, so a stray 0x0e in
    # decoded text cannot silently switch the charset
    proc, screen, stream = make(20, 3)
    feed(stream, "\x1b)0\x0eq\x0f", char_by_char)
    assert screen.buffer[0][0].data == "q", (tag, screen.buffer[0][0].data)

    # multi-byte text is unaffected by a graphics designation
    proc, screen, stream = make(20, 3)
    feed(stream, "\x1b(0\u00e9\u2500", char_by_char)
    row = "".join(screen.buffer[0][x].data for x in range(2))
    assert row == "\u00e9\u2500", (tag, repr(row))

    # shift+enter esc-cr passthrough sanity: ESC CR fed back in draws nothing odd
    proc, screen, stream = make()
    feed(stream, "\x1b[?25l\x1b[?25h", char_by_char)
    assert not screen.cursor.hidden, tag

    # DECSCUSR tracked; missing parameter means 0; reset restores the default
    proc, screen, stream = make()
    feed(stream, "\x1b[2 q", char_by_char)
    assert screen.cursor_style == 2, (tag, screen.cursor_style)
    feed(stream, "\x1b[ q", char_by_char)
    assert screen.cursor_style == 0, (tag, screen.cursor_style)
    feed(stream, "\x1b[6 q", char_by_char)
    screen.reset()
    assert screen.cursor_style == 0, (tag, screen.cursor_style)
    # nothing leaked to the screen or the pty
    assert screen.buffer[0][0].data == " " and proc.written == "", tag

    print("all emulator tests passed ({})".format(tag))


def run_resize_cases():
    """Shrinking must drop cells past the new width, in both buffers: the
    segmenter walks a line to its highest key, so leftovers keep rendering."""
    proc, screen, stream = make(20, 3)
    feed(stream, "ABCDEFGHIJKLMNOPQRST", False)
    screen.resize(3, 10)
    assert max(screen.buffer[0]) < 10, sorted(screen.buffer[0])

    # the buffer parked by the alt screen is trimmed too, or switching back
    # brings the stale cells with it
    proc, screen, stream = make(20, 3)
    feed(stream, "PRIMARYPRIMARYPRIMAR", False)
    feed(stream, "\x1b[?1049h", False)
    screen.resize(3, 8)
    feed(stream, "\x1b[?1049l", False)
    assert max(screen.buffer[0]) < 8, sorted(screen.buffer[0])

    # a shrink clamps the cursor; without the clamp draw() writes to rows
    # above or below the screen
    proc, screen, stream = make(10, 8)
    feed(stream, "\r\n".join("line{}".format(i) for i in range(8)), False)
    feed(stream, "\x1b[1;1H", False)  # full screen, cursor at home
    screen.resize(4, 10)
    assert screen.cursor.y == 0, screen.cursor.y

    proc, screen, stream = make(10, 8)
    feed(stream, "top\x1b[7;1H", False)  # cursor on a blank row below content
    screen.resize(4, 10)
    assert screen.cursor.y == 3, screen.cursor.y

    proc, screen, stream = make(20, 4)
    feed(stream, "x" * 15, False)
    screen.resize(4, 10)
    assert screen.cursor.x == 10, screen.cursor.x

    # mode 2031 is how a live theme change reaches the app
    proc, screen, stream = make()
    assert not screen.theme_notify_enabled()
    feed(stream, "\x1b[?2031h", False)
    assert screen.theme_notify_enabled()

    print("all resize tests passed")


def run_private_csi_cases():
    """No private CSI may raise: an exception here kills the renderer thread
    and freezes the tab. aerc probes XTSMGRAPHICS (ESC[?2;1;0S)."""
    for final in "ABCDEFGHJKLMPSTXZbcdfghlmnqrstu@":
        proc, screen, stream = make()
        stream.feed("\x1b[?2;1;0" + final)          # multi-parameter private CSI
        stream.feed("\x1b[?1" + final)              # single-parameter
        stream.feed("\x1b[?" + final)               # bare

    # the ones we do support still work
    proc, screen, stream = make()
    feed(stream, "\x1b[?1000h\x1b[?1002h\x1b[?1006h", False)
    assert screen.mouse_reporting_enabled(), "private h must still set modes"
    feed(stream, "\x1b[?1000l\x1b[?1002l", False)
    assert not screen.mouse_reporting_enabled(), "private l must still reset modes"

    proc, screen, stream = make()
    feed(stream, "\x1b[5;10H\x1b[?6n", False)
    assert proc.written == "\x1b[?5;10R", proc.written

    # ...and an unsupported one is ignored rather than answered or dispatched
    proc, screen, stream = make()
    feed(stream, "\x1b[?2;1;0S", False)
    assert proc.written == "", "XTSMGRAPHICS must be ignored, not answered"

    print("all private CSI tests passed")


def run_decscnm_cases():
    """A DECSCNM flash (vim's visual bell) must not outlive itself.

    The lines point at the blank cell, so one keeping a reverse default paints
    the columns the app never wrote — vim skips indentation with cursor-forward
    — as white blocks.
    """
    from vterm.render import segment_buffer_line

    # a gutter, then the indent skipped with CSI 8C, then the text: vim's shape
    paint = "\x1b[1;1H\x1b[93m 47 \x1b[m\x1b[8C\x1b[96mreturn\x1b[m default"
    for order in (paint + "\x1b[?5h\x1b[?5l", "\x1b[?5h\x1b[?5l" + paint):
        proc, screen, stream = make(columns=60, lines=5)
        feed(stream, order, False)
        assert not screen.default_char.reverse, "the flash is over"
        assert not screen.buffer[0].default.reverse, "line kept the flash's blank cell"
        assert not screen.buffer[0][6].reverse, "untouched column left reverse"
        painted = [s for s in segment_buffer_line(screen.buffer[0])
                   if not s[0].strip() and s[3] == "reverse_default"]
        assert not painted, ("whitespace painted reverse", painted)

    # while the flash is on, the untouched columns invert too
    proc, screen, stream = make(columns=60, lines=5)
    feed(stream, paint + "\x1b[?5h", False)
    assert screen.buffer[0].default.reverse and screen.buffer[0][6].reverse, \
        "DECSCNM must invert the whole screen, not only the written columns"

    print("all DECSCNM tests passed")


# everything the two paths must agree on: text, SGR in each form, empty and
# clamped params, cursor moves, private modes, prefixed CSI, DECRQM, OSC,
# scroll regions, controls inside a CSI, a colon subparameter, wide chars
GAUNTLET = (
    "plain text\r\n"
    "\x1b[1;31mbold red\x1b[m normal \x1b[38;5;190mpalette\x1b[0m\r\n"
    "\x1b[38;2;10;200;30mtruecolor\x1b[49m\x1b[m\r\n"
    "\x1b[H\x1b[2Kover\twritten\x1b[3;5Hmoved\x1b[999999C clamp\x1b[m\r\n"
    "\x1b[?25l\x1b[?1049h\x1b[?25h\x1b[?1049l"
    "\x1b[>q\x1b[?2026$p\x1b[5n\x1b[6n"
    "\x1b]0;a title\x07\x1b]11;?\x07"
    "\x1b[2;5r\x1b[Sscrolled\x1b[T\x1b[r"
    "\x1b[38:5:100mcolon\x1b[m\r\n"
    "\x1b[2\x073m bel mid-CSI \x1b[3\x18X can cancels\r\n"
    "wide: 漢字 \x1b[7m漢\x1b[m\r\n"
    "\x1b[m\x1b[;5m\x1b[5;m\x1b[25m params\r\n"
)


def run_fast_path_cases():
    """The feed() fast path must be indistinguishable from the parser."""

    def state(screen):
        rows = [
            tuple(screen.buffer[y][x] for x in range(screen.columns))
            for y in range(screen.lines)
        ]
        c = screen.cursor
        return (rows, c.x, c.y, c.attrs, set(screen.mode), screen.title)

    def parser_only(blob):
        """The blob through the coroutine parser alone, bypassing the fast path."""
        proc, screen, stream = make(columns=30, lines=8)
        import pyte
        pyte.Stream.feed(stream, blob)
        return proc, screen

    ref_proc, ref_screen = parser_only(GAUNTLET)
    reference = state(ref_screen)
    # chunk sizes that split every sequence somewhere; 1 exercises pure carry
    for size in (1, 2, 3, 7, 64, len(GAUNTLET)):
        proc, screen, stream = make(columns=30, lines=8)
        for i in range(0, len(GAUNTLET), size):
            stream.feed(GAUNTLET[i : i + size])
        assert state(screen) == reference, "fast path diverged at chunk size {}".format(size)
        assert proc.written == ref_proc.written, (size, proc.written, ref_proc.written)

    # a param run past the carry bound still parses, via the parser
    proc, screen, stream = make()
    stream.feed("\x1b[" + "1;" * 40)
    stream.feed("31mX")
    assert screen.buffer[0][0].fg == "red", screen.buffer[0][0]

    # a carried partial does not linger: nothing dispatches until it completes
    proc, screen, stream = make()
    stream.feed("A\x1b[3")
    assert screen.buffer[0][1].data == " ", "partial CSI must not be drawn"
    stream.feed("1mB")
    assert screen.buffer[0][1].fg == "red" and screen.buffer[0][1].data == "B"

    print("all fast-path tests passed")


def run_malformed_csi_cases():
    """A malformed CSI must cost one sequence, not the chunk.

    Excess params used to TypeError out of the dispatch; feed_data caught it
    and dropped up to 256K of output. Triggered by cat'ing a binary file or a
    corrupted stream. The SGR range guards the blink class the same way.
    """
    for char_by_char in (False, True):  # fast path / parser
        for final in (chr(c) for c in range(0x40, 0x7F)):
            for params in ("2;3", "0;0;0"):
                proc, screen, stream = make(columns=20, lines=6)
                feed(stream, "ab\x1b[{}{}cd".format(params, final), char_by_char)
                row = "".join(screen.buffer[y][x].data for y in range(6) for x in range(20))
                assert "c" in row and "d" in row, \
                    ("output after a malformed CSI was lost", params, final, char_by_char)
        for n in range(121):
            proc, screen, stream = make(columns=20, lines=6)
            feed(stream, "a\x1b[{}mb".format(n), char_by_char)
            assert screen.buffer[0][1].data == "b", ("SGR {} ate the chunk".format(n), char_by_char)

    print("all malformed-CSI tests passed")


def run_clear_cases():
    """Screen wipe and post-clear history suppression (vterm_clear_screen)."""
    proc, screen, stream = make(10, 4)
    feed(stream, "aaa\r\nbbb\r\nccc\r\nddd\r\neee", False)
    assert len(screen.history) > 0
    assert screen.buffer[0][0].data != " "

    screen.wipe()
    rows = ["".join(screen.buffer[y][x].data for x in range(screen.columns)).strip()
            for y in range(screen.lines)]
    assert rows == ["", "", "", ""], rows
    assert (screen.cursor.x, screen.cursor.y) == (0, 0)
    assert screen.dirty == set(range(4)), screen.dirty

    # the app's repaint right after a clear must not refill scrollback
    screen.history.clear()
    feed(stream, "one\r\ntwo\r\nthree\r\nfour\r\nfive", False)
    assert len(screen.history) == 0, len(screen.history)

    # ... but scrollback works again once the window expires
    screen._suppress_history_until = 0
    feed(stream, "\r\nsix\r\nseven", False)
    assert len(screen.history) > 0, "history suppressed for too long"

    # ED 3 is app-driven: it clears scrollback and fires the clear callback,
    # but (as in xterm) leaves the visible screen alone
    cleared = []
    proc = FakeProcess()
    screen = VtScreen(10, 4, process=proc, clear_callback=lambda: cleared.append(1))
    stream = VtStream(screen)
    feed(stream, "gone\r\nalso-gone\r\nmore\r\nlines\r\nx", False)
    assert len(screen.history) > 0
    feed(stream, "\x1b[3J", False)
    assert len(screen.history) == 0 and cleared, (len(screen.history), cleared)
    assert screen.buffer[0][0].data == "a", screen.buffer[0][0]

    # with erase_scrollback off, ED 3 is a no-op: an app repainting with it
    # would take the history and the view with it
    cleared = []
    proc = FakeProcess()
    screen = VtScreen(10, 4, process=proc, erase_scrollback=False,
                      clear_callback=lambda: cleared.append(1))
    stream = VtStream(screen)
    feed(stream, "kept\r\nalso-kept\r\nmore\r\nlines\r\nx", False)
    kept = len(screen.history)
    assert kept > 0
    del cleared[:]  # the screen's own reset() fired one at construction
    feed(stream, "\x1b[3J", False)
    assert len(screen.history) == kept and not cleared, (len(screen.history), cleared)
    assert screen.buffer[0][0].data == "a", screen.buffer[0][0]

    print("all clear-screen tests passed")


run_case(char_by_char=False)
run_case(char_by_char=True)
run_clear_cases()
run_resize_cases()
run_private_csi_cases()
run_decscnm_cases()
run_fast_path_cases()
run_malformed_csi_cases()
