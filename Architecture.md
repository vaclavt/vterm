
## Files

### The package

| File | Role |
|---|---|
| `main.py` | Entry point. Imports every command and listener class so Sublime discovers them, and defines `plugin_loaded()`. |
| `vterm/terminal.py` | `Terminal`: pty process, emulator ownership, the reader/renderer threads, and everything written back to the app. |
| `vterm/emulator.py` | `VtScreen` and `VtStream`: pyte subclasses adding the terminal features these agents need. |
| `vterm/render.py` | Screen → view text, colour overlays, caret, folding, trimming. |
| `vterm/scheme.py` | `SchemeManager`: generates one theme's colour scheme, the scopes the renderer paints with. `get_scheme_manager` caches one instance per theme name, each terminal frozen to the instance it was opened or rethemed with. |
| `vterm/themes.py` | The built-in palettes (`default`, `homebrew`, `pro`, `slate`, `red-alert`). |
| `vterm/commands.py` | The Sublime commands: open, keypress, copy/paste, theme picker, clear, cleanup. |
| `vterm/listeners.py` | `VtermCoreEventListener`: focus, IME capture, copy/paste rerouting, the mousemap context. |
| `vterm/mouse.py` | Wheel and click handling. |
| `vterm/keys.py` | Key name + modifiers → escape sequence tables. |
| `vterm/utils.py` | `settings()`, the `responsive` throttle, the `intermission` pacer, `rev_wcwidth`. |

### Sublime-facing files at the repo root

| File | Role |
|---|---|
| `vterm.sublime-settings` | Defaults for the twelve plugin settings. |
| `vterm.sublime-commands` | Four palette entries: open, open terminal in project folder, clear screen, select theme. |
| `vterm.sublime-syntax` | An intentionally empty hidden syntax, so terminal text is never tokenized. |
| `Default.sublime-keymap` | 245 generated bindings. **Do not edit by hand** — regenerate with `tools/gen_keymap.py`. |
| `Default (OSX).sublime-mousemap` | Two wheel bindings and the middle button. |
| `dependencies.json` | Package Control libraries. |

Generated at runtime, not in the repo: `Packages/User/vterm/vterm-<theme>.hidden-color-scheme`,
one per theme (`vterm-system` for "no theme").

## Classes

- **`Terminal`** (`terminal.py`) — one per terminal tab. Owns the pty, the `VtScreen` and
  `VtStream`, and the two threads. Instances live in the class-level registry
  `Terminal._terminals`, keyed by **view id**; `Terminal.from_id(view.id())` is how every
  command and listener finds its terminal, and it is a plain dict lookup, so it is the
  cheapest possible gate to put at the top of a hook.
- **`TerminalPtyProcess`** (`terminal.py`) — `ptyprocess.PtyProcess` that accepts `str`.
- **`VtScreen`** (`emulator.py`) — `pyte.Screen` plus the alternate screen, scrollback,
  synchronized output, mouse/focus/paste modes, cursor styles, query replies, OSC
  handling. Mutated only by the renderer thread, so it carries no lock.
- **`VtStream`** (`emulator.py`) — `pyte.Stream` with a rewritten parser FSM. Stock pyte
  drops what these agents rely on: `>`/`<`/`=` prefixed CSI, the DECRQM `$p` intermediate,
  and OSC codes longer than one digit. Its `feed()` regex-matches complete plain CSI and
  dispatches them directly (params memoized) — pyte resumes the parser coroutine once per
  character, which dominated escape-heavy floods. OSC, charsets, private/prefixed CSI and
  `:` subparams still take the coroutine, byte for byte; a read ending mid-CSI is carried
  into the next one. Parity is enforced by a differential test (`run_fast_path_cases`).
- **`Cursor`** (`emulator.py`) — replaces pyte's so the default attribute set can be a
  `Char` carrying the extra `linefeed` field.
- **`SchemeManager`** (`scheme.py`) — one per theme name, cached by `get_scheme_manager`.
  Owns the generated scheme file and the mapping from colour combination to scope name.
- **`VtermRenderCommand`** (`render.py`) — the per-frame logic. Sublime rebuilds every
  `TextCommand` instance whenever any plugin loads, so the view's render state
  (`RenderState`: `colored_lines`, `row_cache`, `key_counter`, the scrollback chunk
  bookkeeping `archived_chunks`, `open_rows`, `open_chunk`, `trimmed`) lives on
  `terminal.render_state`, bound as `self.state` at the top of each `run()`.

### Commands

| Command | Class | Reached from |
|---|---|---|
| `vterm_open` | `VtermOpenCommand` | palette, user keybindings |
| `vterm_open_terminal_project_folder` | `VtermOpenTerminalProjectFolderCommand` | palette |
| `vterm_initialize_view` | `VtermInitializeViewCommand` | internal |
| `vterm_keypress` | `VtermKeypressCommand` | 244 keymap bindings |
| `vterm_render` | `VtermRenderCommand` | renderer thread |
| `vterm_show_cursor` | `VtermShowCursorCommand` | internal |
| `vterm_copy` / `vterm_paste_text` | `commands.py` | `on_text_command`, IME capture |
| `vterm_paste_selection` | `commands.py` | mousemap, middle button |
| `vterm_select_theme` | `VtermSelectThemeCommand` | palette |
| `vterm_clear_screen` | `VtermClearScreenCommand` | palette, `super+shift+k` |
| `vterm_cleanup` | `VtermCleanupCommand` | renderer, on process exit |
| `vterm_clear_undo_stack` / `vterm_noop` | `commands.py` | internal |
| `vterm_scroll` / `vterm_mouse_press` / `vterm_mouse_release` | `mouse.py` | mousemap |

One application-wide `EventListener`: `VtermCoreEventListener` (`listeners.py`).


---

## Terminal concepts, briefly

For anyone who has not worked with terminal protocols before. The ones vterm implements
are marked ✅.

**Control bytes** — single characters below 0x20 with an effect rather than a glyph: CR
(`\r`) return to column 0, LF (`\n`) next line, BS backspace, HT tab, BEL bell.

**Escape sequence** — `ESC` (0x1B) followed by more bytes. Everything below is one.

- **CSI** ✅ — Control Sequence Introducer, `ESC [`. The workhorse: `ESC[2J` clear screen,
  `ESC[10;5H` move the cursor. Ends with a *final byte* that names the operation;
  numbers in between are its parameters.
- **SGR** ✅ — Select Graphic Rendition, `CSI … m`. Sets colour and style: `ESC[1m` bold,
  `ESC[31m` red foreground, `ESC[38;5;208m` a 256-palette colour, `ESC[38;2;r;g;bm`
  truecolor, `ESC[0m` reset. This is what TUIs emit most.
- **OSC** ✅ — Operating System Command, `ESC ]`, ending at BEL or ST. Carries strings
  rather than numbers: `OSC 0/2` set the window title, `OSC 10/11` query the default
  foreground/background, `OSC 52` reads or writes the system clipboard (vterm
  ignores it: untrusted output must not plant a paste).
- **DCS / APC** — Device Control String (`ESC P`) and Application Program Command
  (`ESC _`). Long payloads: sixel images, kitty graphics. vterm parses their framing and
  discards the payload, so an image never leaks into the text.
- **SS3** ✅ — `ESC O`, a single shifted character. Cursor keys take this form in
  application mode; F1-F4 use it too.
- **ST** — String Terminator, `ESC \`, which ends OSC/DCS/APC payloads (BEL also ends OSC).

**Modes** are switches. DEC *private* modes are set with `CSI ? n h` (DECSET) and cleared
with `CSI ? n l` (DECRST); **DECRQM** ✅ (`CSI ? n $p`) asks whether one is on, which is
how an app discovers what the terminal supports.

- **1049** ✅ alternate screen — a second, scrollback-less buffer for full-screen apps, so
  leaving vim restores your shell exactly as it was.
- **2026** ✅ synchronized output — "I am mid-frame, do not draw yet"; what makes a
  fullscreen UI update without tearing.
- **2004** ✅ bracketed paste — pasted text is wrapped in `ESC[200~`/`ESC[201~` so an app
  can tell it apart from typing and not auto-indent it.
- **1000 / 1002 / 1003** ✅ mouse reporting — clicks / clicks with drag / all motion.
- **1006** ✅ SGR mouse encoding — the modern wire form, `ESC[<button;col;rowM`, without
  the 223-column limit of the original.
- **1004** ✅ focus reporting — `ESC[I` on focus, `ESC[O` on blur.
- **DECCKM** ✅ — application cursor keys: arrows switch from `CSI A` to `SS3 A`.
- **DECAWM** ✅ — autowrap at the right margin.
- **DECSCNM** ✅ — reverse video for the whole screen.
- **DECSCUSR** ✅ (`CSI n SP q`) — caret shape: block, underline or bar, blinking or not.
- **DECSTBM** ✅ (`CSI t;b r`) — set the scrolling region to a band of rows.
- **DECKPAM** — application keypad mode, the numeric keypad's equivalent of DECCKM.
  vterm ignores it.
- **LNM** ✅ — line feed/new line mode; decides whether Enter sends CR or CR LF.

**Queries** — the app asks, the terminal answers *on the input stream*, as if typed:

- **DA1 / DA2** ✅ (`CSI c`, `CSI > c`) — Device Attributes: what kind of terminal are you,
  and what version. Claude Code waits on DA1 with no timeout, so failing to answer hangs it.
- **DSR / CPR** ✅ (`CSI n`, `CSI 6n`) — Device Status Report / Cursor Position Report:
  "are you ok" and "where is the cursor". Private `DSR 996` reports whether the palette is
  light or dark.
- **XTVERSION** ✅ (`CSI > q`) — the terminal's name and version. Naming ourselves is what
  lets apps enable 2026 on a generic `TERM`.
- **Kitty keyboard protocol** — a richer key encoding with release events. vterm answers
  the probe (`CSI ? u`) with zero flags, so apps fall back to something else — for pi that
  is modifyOtherKeys, which it then enables unprompted.
- **modifyOtherKeys** ✅ (`CSI > 4 ; n m`) — xterm's pre-kitty answer to the same problem:
  keys whose modified form has no legacy encoding are sent as `CSI 27 ; <mod> ; <code> ~`.
  While an app has it on, vterm uses that form for modified enter, tab, backspace and space —
  Shift+Enter is `CSI 27;2;13~`, since a bare `ESC CR` is indistinguishable from Alt+Enter.
  Ctrl+letters keep their control bytes, so Ctrl+C stays `\x03`.

**Other terms**

- **pty** — pseudo-terminal: a kernel device pair. The child thinks its stdin/stdout is a
  terminal; we hold the master end and read what it writes.
- **TIOCSWINSZ / SIGWINCH** ✅ — telling the pty its new size delivers SIGWINCH to the
  child, which is how a resize reaches the app.
- **wcwidth** ✅ — how many columns a character occupies: 1 for ASCII, 2 for CJK and most
  emoji, 0 for combining marks. Terminals are grids, so this has to agree with the font
  or the caret drifts.
- **IME** — Input Method Editor: the OS layer that turns keystrokes into composed text
  (pinyin → 中文, dead keys → accents). It commits text without a matching key event,
  which is why `on_modified` exists.
- **TERM / COLORTERM** ✅ — environment variables describing the terminal's capabilities.
  Children get `TERM=xterm-256color` and `COLORTERM=truecolor`.
