""" Terminal lifecycle: pty process, emulator state, reader/renderer threads """
import codecs
import logging
import os
import threading
import time
from collections import deque
from queue import Empty, Queue

import sublime
from ptyprocess import PtyProcess
from pyte import modes as mo

from .emulator import APP_CURSOR_MODE, VtScreen, VtStream
from .keys import get_key_code
from .scheme import scheme_manager
from .utils import intermission, responsive, settings

LOG = logging.getLogger(__name__)

# renderer tick, ~33 fps
RENDER_PERIOD = 0.03

# draw rate for a visible terminal the user is not typing in, ~15 fps
UNFOCUSED_RENDER_PERIOD = 0.075

# max chars parsed per tick, so a giant burst still renders intermediate frames
FEED_LIMIT = 256 * 1024

# SUPPRESS_HISTORY_AFTER_CLEAR in emulator.py must outlast this
REPAINT_NUDGE_DELAY = 0.25


def view_size(view, default=None):
    """Terminal (rows, columns) that fit the view's viewport."""
    pixel_width, pixel_height = view.viewport_extent()
    pixel_per_line = view.line_height()
    pixel_per_char = view.em_width()

    if pixel_per_line == 0 or pixel_per_char == 0:
        return default or (1, 1)

    # -3 columns: scrollbar and padding fudge
    nb_columns = max(int(pixel_width / pixel_per_char) - 3, 1)
    nb_rows = max(int(pixel_height / pixel_per_line), 1)

    if nb_columns == 1 and default:
        return default

    nb_columns = min(
        max(nb_columns, settings().get("min_columns", 20)),
        settings().get("max_columns", 500),
    )

    return (nb_rows, nb_columns)


def agent_env(overrides=None):
    """Environment for a vterm child process"""
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.pop("TERM_PROGRAM", None)
    env.pop("TERM_PROGRAM_VERSION", None)
    env.setdefault("LANG", "en_US.UTF-8")
    env.update(overrides or {})
    return env


class TerminalPtyProcess(PtyProcess):
    """PtyProcess that takes str; ptyprocess itself only writes bytes."""

    def write(self, s):
        """Encode text and write it to the pty."""
        b = s.encode("utf-8", "backslashreplace")
        return super().write(b)


class Terminal:
    """One terminal: its pty, emulator and the threads that keep the view current.

    Instances live in the class-level registry keyed by view id, which is why
    the plugin never purges its submodules on reload — rebuilding the registry
    would orphan every open tab.
    """

    _terminals = {}

    def __init__(self, view):
        """State only; the pty and the threads come up in start()."""
        self.view = view
        # process.isalive() costs two waitpid syscalls; UI hooks read this instead
        self.alive = True
        self.default_title = ""
        self.offset = 0
        self.viewport_y = 0
        self.scrollback_size = 50000
        self._hosted = True
        self._hosted_checked = 0
        self.focused = True
        self._visible = True
        self._visible_checked = 0
        self._rendered = 0
        self._cached_cursor = (0, 0, False, 0)
        self.applied_alt_screen = False
        self._strings = Queue()
        self._pending_to_send_string = False
        # guards the queue/flag handoff between send_string and the drain thread
        self._send_lock = threading.Lock()
        self._wheel_captured = False
        self._scroll_pending = False
        self.pending_clear = False
        self.pending_clear_screen = False
        # a forced frame (set on activation); rendering stays on the renderer
        # thread, which owns the emulator
        self.pending_render = False
        # a UTF-8 sequence split across read chunks must not be dropped
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    @classmethod
    def from_id(cls, vid):
        """The terminal behind a view id, or None once it is gone."""
        return cls._terminals.get(vid)

    def is_hosted(self):
        """Window lookup, throttled per terminal (a shared @responsive closure would rate-limit all of them)"""
        now = time.time()
        if now - self._hosted_checked > 1:
            self._hosted_checked = now
            self._hosted = self.view.window() is not None
        return self._hosted

    def is_visible(self):
        """Whether this tab is the front one in its group, throttled like is_hosted.

        Selecting another tab in a group that does not hold focus raises no
        event for this view, so the flag has to be polled.
        """
        if self.focused:
            return True
        now = time.time()
        if now - self._visible_checked > 1:
            self._visible_checked = now
            window = self.view.window()
            group = window.get_view_index(self.view)[0] if window else -1
            self._visible = group >= 0 and window.active_view_in_group(group) == self.view
        return self._visible

    def is_render_due(self):
        """Whether an unfocused terminal may draw again.

        A frame costs a command dispatch and a dozen UI round trips however
        little changed, so a terminal the user is only watching gets them at a
        lower rate than one they are typing in.
        """
        if self.focused:
            return True
        now = time.time()
        if now - self._rendered < UNFOCUSED_RENDER_PERIOD:
            return False
        self._rendered = now
        return True

    def start(self, cmd, cwd=None, env=None, default_title=None):
        """Spawn the pty sized to the view, build the emulator, start the threads."""
        view = self.view
        Terminal._terminals[view.id()] = self
        self.default_title = default_title

        size = view_size(view, default=(40, 80))
        LOG.debug("[start] cmd=%s cwd=%s size=%s", cmd, cwd, size)

        self.process = TerminalPtyProcess.spawn(cmd, cwd=cwd, env=agent_env(env), dimensions=size)
        # one source for the deque and the view row cap (alt screen zeroes the deque)
        self.scrollback_size = settings().get("scrollback_history_size", 50000)
        self.screen = VtScreen(
            size[1],
            size[0],
            process=self.process,
            history=self.scrollback_size,
            erase_scrollback=settings().get("honor_erase_scrollback", False),
            clear_callback=self._clear_callback,
            clipboard_callback=self._clipboard_callback,
            fg_color=scheme_manager.foreground.lstrip("#"),
            bg_color=scheme_manager.background.lstrip("#"),
            is_dark=scheme_manager.is_dark(),
        )
        self.stream = VtStream(self.screen)
        scheme_manager.add_listener(self.apply_scheme)

        self._start_rendering()

    def apply_scheme(self):
        """Adopt a new palette; mode 2031 subscribers are told so they can restyle"""
        # the screen answers OSC 10/11 and DSR 996 from these values
        screen = self.screen
        screen.default_fg_color = scheme_manager.foreground.lstrip("#")
        screen.default_bg_color = scheme_manager.background.lstrip("#")
        screen.theme_is_dark = scheme_manager.is_dark()
        if screen.theme_notify_enabled():
            self.send_string(screen.theme_report(), normalized=False)

    def kill(self):
        """Terminate the child and drop this terminal from the registry."""
        LOG.debug("[kill] terminating process")
        self.alive = False
        scheme_manager.remove_listener(self.apply_scheme)
        try:
            self.process.terminate()
        except Exception:
            LOG.warning("terminate failed", exc_info=True)
        vid = self.view.id()
        if vid in Terminal._terminals:
            del Terminal._terminals[vid]

    def _clear_callback(self):
        """Screen asked for a clear; the next render performs it on the UI thread."""
        self.pending_clear = True

    def _clipboard_callback(self, text):
        """OSC 52 payload; the clipboard may only be touched on the UI thread."""
        sublime.set_timeout(lambda: sublime.set_clipboard(text))

    def _need_to_render(self):
        """Whether anything changed since the last frame: dirty rows, a clear, or the cursor."""
        screen = self.screen
        if self.pending_render or self.pending_clear or self.pending_clear_screen:
            # a pending clear or forced frame must render even when the app is idle
            self.pending_render = False
            return True
        cursor = (screen.cursor.x, screen.cursor.y, screen.cursor_hidden(), screen.cursor_style)
        if screen.dirty or cursor != self._cached_cursor:
            self._cached_cursor = cursor
            return True
        return False

    def _start_rendering(self):
        """Start the reader and renderer threads that drive this terminal."""
        # deque appends/poplefts are atomic, so the reader never blocks behind
        # a render; a lock here used to backpressure the pty for whole frames
        chunks = deque()
        done = [False]

        @responsive(period=1, default=False)
        def was_resized():
            """Whether the viewport no longer matches the screen. Costs sublime API calls, so once a second."""
            size = view_size(self.view)
            return self.screen.lines != size[0] or self.screen.columns != size[1]

        def reader():
            """Drain the pty into `chunks` until EOF or the view goes away."""
            while True:
                try:
                    # ptyprocess buffers 8K; smaller reads double the syscalls
                    raw = self.process.read(8192)
                except EOFError:
                    break
                chunks.append(self._decoder.decode(raw))
                if done[0] or not self.is_hosted():
                    LOG.debug("[reader] stops")
                    break
            self.alive = False
            done[0] = True

        def feed_data():
            """Parse up to FEED_LIMIT of pending output into the screen."""
            parts = []
            size = 0
            while chunks and size < FEED_LIMIT:
                part = chunks.popleft()
                parts.append(part)
                size += len(part)
            if parts:
                try:
                    self.stream.feed("".join(parts))
                except Exception:
                    # pyte resets its parser before re-raising, so the terminal
                    # can carry on; letting this escape would kill the thread
                    LOG.exception("stream.feed failed, dropping the rest of a chunk")

        def renderer():
            """Own the emulator: parse, then ask sublime to redraw, once per tick."""
            settled_y = None
            while True:
                # a hidden tab is not drawn at all: the screen keeps the state
                # and one render on unhide catches the view up. The tick stays at
                # full rate either way, or the parser would fall behind the pty
                visible = self.is_visible()
                with intermission(period=RENDER_PERIOD):
                    feed_data()
                    if self.screen.in_sync_frame():
                        # mid ?2026 frame: wait for the closing sequence
                        continue
                    if visible:
                        if was_resized():
                            self.handle_resize()
                            self.view.run_command("vterm_show_cursor")
                        # due first: _need_to_render consumes the cursor move it reports
                        if self.is_render_due() and self._need_to_render():
                            self.view.run_command("vterm_render")
                            # a frame can move the viewport: a sample from
                            # before it says nothing about coming to rest
                            settled_y = None
                        else:
                            # the wheel can drag the screen off the top into
                            # sublime's past-end room; a frame undoes that, and
                            # an idle terminal draws none. Pinning while the
                            # view is still gliding fights the scroll's
                            # momentum, which reads as shaking, so wait for two
                            # ticks to agree that it has come to rest
                            y = self.view.viewport_position()[1]
                            if y == settled_y and self.viewport_y < y:
                                from .render import request_scroll  # module-level import would cycle
                                request_scroll(self.view, self)
                            settled_y = y
                    self._sync_wheel_flag()
                    if done[0] or not self.is_hosted():
                        LOG.debug("[renderer] stops")
                        break

            while chunks:
                feed_data()
            done[0] = True
            sublime.set_timeout(lambda: self.view.run_command("vterm_cleanup"))

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=renderer, daemon=True).start()

    def handle_resize(self):
        """Match the pty and the screen to the view, which signals SIGWINCH to the app."""
        size = view_size(self.view)
        LOG.debug("[resize] %sx%s -> %sx%s", self.screen.lines, self.screen.columns, size[0], size[1])
        try:
            self.process.setwinsize(*size)
            self.screen.resize(*size)
        except OSError:
            # pty already gone; cleanup follows via the reader thread
            LOG.warning("resize failed", exc_info=True)

    def nudge_repaint(self):
        """Make the app repaint by briefly narrowing the pty. Never write ctrl+L instead: it is /clear in Claude Code"""
        rows, columns = self.screen.lines, self.screen.columns
        self._set_winsize(rows, max(columns - 1, 1))
        sublime.set_timeout(lambda: self._set_winsize(rows, columns), int(REPAINT_NUDGE_DELAY * 1000))

    def _set_winsize(self, rows, columns):
        """Resize the pty alone, leaving the screen as it is."""
        try:
            self.process.setwinsize(rows, columns)
        except OSError:
            LOG.warning("setwinsize failed", exc_info=True)

    def send_key(self, **kwargs):
        """Encode a key name plus modifiers for the app's current modes and send it."""
        kwargs["application_mode"] = APP_CURSOR_MODE in self.screen.mode
        kwargs["new_line_mode"] = mo.LNM in self.screen.mode
        kwargs["modify_other_keys"] = self.screen.modify_other_keys
        self.send_string(get_key_code(**kwargs), normalized=False)

    def send_string(self, string, normalized=True):
        """Write text to the pty, chunking anything large.

        Args:
            normalized: canonicalise line endings for the app's LNM setting;
                off for escape sequences, which must go through untouched.
        """
        if normalized:
            string = string.replace("\r\n", "\n")
            if mo.LNM in self.screen.mode:
                string = string.replace("\n", "\r\n")
            else:
                string = string.replace("\n", "\r")

        # chunked so a big paste cannot overrun the pty buffer; the blocking
        # pty write is the backpressure, no pacing sleep needed
        with self._send_lock:
            no_queue = not self._pending_to_send_string
            if not (no_queue and len(string) <= 4096):
                for i in range(0, len(string), 4096):
                    self._strings.put(string[i : i + 4096])
                if no_queue:
                    self._pending_to_send_string = True
                    threading.Thread(target=self._process_send_string, daemon=True).start()
                return
        # outside the lock: the pty write can block on backpressure
        self.process.write(string)

    def _process_send_string(self):
        """Drain the queued paste chunks; the blocking pty write paces them."""
        while True:
            try:
                # linger so a burst of sends does not respawn the thread
                string = self._strings.get(timeout=0.2)
            except Empty:
                # under the lock: a chunk enqueued after the timeout but before
                # the flag drops would otherwise strand until the next send
                with self._send_lock:
                    if self._strings.empty():
                        self._pending_to_send_string = False
                        return
                continue
            self.process.write(string)

    def _sync_wheel_flag(self):
        """Publish whether the app owns the wheel, for the mousemap context."""
        # the mousemap routes the wheel to the app while it reports the mouse
        # or owns the alt screen; VtermScrollCommand picks the encoding
        captured = self.screen.mouse_reporting_enabled() or self.screen.alternate_buffer_mode
        if captured != self._wheel_captured:
            self._wheel_captured = captured
            self.view.settings().set("vterm_view.wheel_captured", captured)

    def send_focus(self, focused):
        """Report focus gain or loss, when the app asked for it (?1004)."""
        if self.screen.focus_tracking_enabled():
            self.send_string("\x1b[I" if focused else "\x1b[O", normalized=False)

    def send_mouse(self, button, col, row, pressed):
        """Emit an SGR mouse report (1-based col/row)."""
        if not self.screen.mouse_reporting_enabled():
            return
        self.send_string("\x1b[<{};{};{}{}".format(button, col, row, "M" if pressed else "m"), normalized=False)
