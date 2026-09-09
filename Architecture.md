# vterm architecture

How the plugin is put together, for someone about to change it. `README.md` is the
user-facing overview; this is the developer reference. Code style lives in `CODE.md`.

## What it is

A terminal emulator that runs inside an ordinary Sublime Text tab. There is no custom
drawing surface: the emulator's screen is written into the view as **real text**, and
colours are region overlays. That single decision explains most of what follows.

Constraints it works under:

- **Sublime Text 4**, build 4149+ (`View.folded_regions`), plugin host **Python 3.8**
  (`.python-version`). The host is a separate process, so every Sublime API call from
  plugin code is a cross-process round trip — call *count* is what costs.
- **macOS only.** The pty layer is posix and the mousemap is `Default (OSX)`.
- Third-party code comes from Package Control libraries declared in `dependencies.json`:
  `pyte` (emulation), `ptyprocess` (pty), `wcwidth` (character widths).
- Editing plugin sources needs a **Sublime restart**: submodules are deliberately not
  purged on reload, because rebuilding the terminal registry would orphan open tabs.

## Files

### The package

| File | Role |
|---|---|
| `main.py` | Entry point. Imports every command and listener class so Sublime discovers them, and defines `plugin_loaded()`. |
| `vterm/terminal.py` | `Terminal`: pty process, emulator ownership, the reader/renderer threads, and everything written back to the app. |
| `vterm/emulator.py` | `VtScreen` and `VtStream`: pyte subclasses adding the terminal features these agents need. |
| `vterm/render.py` | Screen → view text, colour overlays, caret, folding, trimming. |
| `vterm/scheme.py` | `SchemeManager`: generates the colour scheme that defines the scopes the renderer paints with. |
| `vterm/themes.py` | The built-in palettes (`default`, `homebrew`, `pro`, `slate`). |
| `vterm/commands.py` | The Sublime commands: open, keypress, copy/paste, theme picker, clear, cleanup, close. |
| `vterm/listeners.py` | `VtermCoreEventListener`: focus, IME capture, copy/paste rerouting, the mousemap context. |
| `vterm/mouse.py` | Wheel and click handling. |
| `vterm/keys.py` | Key name + modifiers → escape sequence tables. |
| `vterm/utils.py` | `settings()`, the `responsive` throttle, the `intermission` pacer, `rev_wcwidth`. |

### Sublime-facing files at the repo root

| File | Role |
|---|---|
| `vterm.sublime-settings` | Defaults for the eleven plugin settings. |
| `vterm.sublime-commands` | Four palette entries: open, open terminal in project folder, clear screen, select theme. |
| `vterm.sublime-syntax` | An intentionally empty hidden syntax, so terminal text is never tokenized. |
| `Default.sublime-keymap` | 245 generated bindings. **Do not edit by hand** — regenerate with `tools/gen_keymap.py`. |
| `Default (OSX).sublime-mousemap` | Two wheel bindings. |
| `dependencies.json` | Package Control libraries. |

Generated at runtime, not in the repo: `Packages/User/vterm/vterm.hidden-color-scheme`.

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
- **`SchemeManager`** (`scheme.py`) — module singleton `scheme_manager`. Owns the
  generated scheme file and the mapping from colour combination to scope name.
- **`VtermRenderCommand`** (`render.py`) — Sublime keeps **one command instance per
  view**, so its `colored_lines`, `_row_cache`, `_key_counter`, the scrollback chunk
  bookkeeping (`_archived_chunks`, `_open_rows`, `_open_chunk`, `_trimmed`) and
  `_lastrow` are that view's render state.

### Commands

| Command | Class | Reached from |
|---|---|---|
| `vterm_open` | `VtermOpenCommand` | palette, user keybindings |
| `vterm_open_terminal_project_folder` | `VtermOpenTerminalProjectFolderCommand` | palette |
| `vterm_initialize_view` | `VtermInitializeViewCommand` | internal |
| `vterm_keypress` | `VtermKeypressCommand` | 244 keymap bindings |
| `vterm_render` | `VtermRenderCommand` | renderer thread |
| `vterm_show_cursor` | `VtermShowCursorCommand` | internal |
| `vterm_copy` / `vterm_paste` / `vterm_paste_text` | `commands.py` | `on_text_command`, IME capture |
| `vterm_select_theme` | `VtermSelectThemeCommand` | palette |
| `vterm_clear_screen` | `VtermClearScreenCommand` | palette, `super+shift+k` |
| `vterm_cleanup` | `VtermCleanupCommand` | renderer, on process exit |
| `vterm_close` | `VtermCloseCommand` | nothing — available for a user keybinding |
| `vterm_clear_undo_stack` / `vterm_noop` | `commands.py` | internal |
| `vterm_scroll` / `vterm_mouse_click` | `mouse.py` | mousemap, `drag_select` interception |

Two application-wide `EventListener`s: `VtermCoreEventListener` (`listeners.py`) and
`VtermMouseEventListener` (`mouse.py`).

## Data flows

### Opening a terminal

`vterm_open` → resolve `cmd` (default `$SHELL -l`) and `cwd` (first project folder →
active file's directory → `~`) → `scheme_manager.initialize()` → `window.new_file()` →
`vterm_initialize_view` stamps the view settings → `Terminal(view)` → `Terminal.start()`:
register in `_terminals`, compute the size from the viewport, spawn the pty, build the
`VtScreen`/`VtStream`, subscribe to palette changes, start the two threads. All on the UI
thread; a failure closes the view again.

### Output: a pty byte becomes a visible character

```
child writes
   │
   ├─ reader thread ──────────────────────────────────────────────
   │   process.read(8192)          blocking read on the pty master
   │   self._decoder.decode(raw)   incremental UTF-8: a sequence split
   │                               across reads must not be corrupted
   │   chunks.append(text)         collections.deque, no lock
   │
   ├─ renderer thread, every 30 ms (RENDER_PERIOD) ───────────────
   │   feed_data()                 popleft up to FEED_LIMIT (256 KiB)
   │   stream.feed(joined)         → VtStream parser → VtScreen.draw
   │                                 mutates buffer, fills screen.dirty
   │   in_sync_frame()?            mid ?2026 frame → skip this tick
   │   is_visible()?               hidden tab → draw nothing this tick
   │   was_resized()?              (checked once a second) → resize pty
   │   is_render_due()?           unfocused → at most one frame per
   │                               UNFOCUSED_RENDER_PERIOD
   │   _need_to_render()?          dirty rows, a pending clear, or a
   │                               moved cursor
   │   view.run_command("vterm_render")
   │   _sync_wheel_flag()          publishes the mousemap gate
   │
   └─ UI thread ──────────────────────────────────────────────────
       VtermRenderCommand.run(edit)
         update_lines()   → drain screen.history above `offset`,
                            then each dirty screen row
           update_line()  → segment_buffer_line() → row-cache check
                            → view.replace() + add_regions()
         sync_alt_fold()
         follow-scroll gate → trim, focus_cursor()*, request_scroll()
         sync_cursor_style(), set_name()*, dirty.clear()
                            * focused terminals only
```

**Rendering off focus.** A terminal only touches its view the way the user would while it
holds focus. Sublime dispatches view events application-wide, and packages hang global
state off them: Vintage's `InputStateTracker.on_selection_modified` resets one module-level
`g_input_state` for every view in every window, so a caret this plugin moved at 33 fps
erased the half-typed `d` of a `dd` in whatever file the user was editing. Sublime drops a
pending key sequence over the same kind of traffic, which took `gg` and `ciw` with it.

Three states, from `Terminal.focused` (set by `on_activated` / `on_deactivated`) and
`Terminal.is_visible()` (polled once a second, because selecting another tab in an
unfocused group raises no event for this view):

| state | text | caret, `sel` | tab name | follow-scroll | draw rate |
| --- | --- | --- | --- | --- | --- |
| focused | yes | yes | yes | yes | 33 fps |
| visible, unfocused | yes | no | no | yes | 15 fps |
| hidden | no | no | no | no | none |

A frame costs a command dispatch and a dozen UI round trips whatever changed — two
`rowcol(size())`, `folded_regions()`, `viewport_position()`, the `trim_trailing_spaces` scan,
then `layout_extent()` / `text_to_layout()` / `set_viewport_position()` — so the rate a
terminal is drawn at is what the editor pays for it, and only a focused one is worth 33 fps.

The renderer tick itself stays at `RENDER_PERIOD` in every state: `feed_data()` runs inside it,
and throttling that would cap the parser at `FEED_LIMIT` per tick while the reader keeps filling
an unbounded `chunks`. Only the drawing is rate-limited. So the emulator is always current and
one render on activation catches the view up. An unfocused terminal is left with an empty selection —
nothing a render edit can shift, so no selection event of ours escapes — unless the user
selected text to copy, which is kept.

**The view/offset model.** The view holds real text. Rows `0 .. offset-1` are scrollback;
rows `offset .. offset + screen.lines - 1` are the live screen. `terminal.offset` is the
view row of screen row 0. When the emulator retires lines into `screen.history`, the
renderer drains them, advances `offset` by that many rows, and writes them at their now
permanent view rows. **Once a line leaves `screen.history`, its only copy is the view
text** — which is why the alternate screen *folds* the scrollback rather than deleting it.

**The row cache.** pyte's `dirty` set is a repaint *hint*, not a diff: scrolling one line
marks every row dirty, and a TUI repaints whole frames. `update_line` compares
`(segments, lf)` against `_row_cache` and returns early when unchanged, which is what
keeps a full-screen repaint from costing hundreds of cross-process calls.

**The follow-scroll gate.** `following()` compares the viewport against
`terminal.viewport_y`, where the last auto-scroll left it. Above that, the user scrolled
up by hand, and trimming, caret placement and scrolling are all skipped so their position
survives the next frame. `scroll_view` (the PgUp/PgDn and Up/Down chords) closes the gate
the same way, and scrolling down clamps to the anchor `bottom_y` returns, which re-opens
it. A one-row scroll lands exactly on the tolerance boundary, so one Up closes the gate
and one Down re-opens it.

**Pinning waits for rest.** The renderer pins a viewport dragged into sublime's past-end
room on a terminal too idle to draw a frame, but only once two ticks agree it has come to
rest: macOS keeps scrolling after the fingers lift, and a hard `set_viewport_position`
against that momentum yanks the view back for as long as the glide lasts.

### How colour is actually drawn

`view.add_regions(key, regions, scope)` can only name a **scope**. It cannot take a
colour. The colours must already exist in the view's colour scheme. So vterm generates
the colour scheme at runtime:

1. `segment_buffer_line` splits a screen row into runs of identical attributes.
2. For each non-default run, `scheme_manager.scope(fg, bg, style)` returns a scope name
   such as `vterm.red.default.b`. On a **miss** it creates the rule and schedules a
   write; the hot path is a single lock-free dict read.
3. `colorize_line` calls `add_regions` once per distinct scope in the row, with keys
   `vterm#N` — monotonic rather than per-row, because `trim_history` renumbers rows and
   per-row keys would collide. `colored_lines[row]` remembers them so they can be erased.
   Rows that scroll off the screen are queued by `archive_rows` and merged by
   `flush_chunk` onto shared `vterm@chunk@scope` keys (one per scope per `CHUNK` rows
   of scrollback), their per-row keys erased — every key is a separate pass in Sublime's
   redraw, and a long coloured session used to accumulate ~15k of them. The merge waits
   for the chunk to close: `add_regions` replaces a key's whole set, so merging every
   frame re-sent everything the chunk held so far, which is quadratic in `CHUNK` when
   output arrives a few rows at a time. The open chunk's rows keep their per-row keys
   until then, so the live count is scopes x chunks plus at most one chunk's worth.
   Chunk ordinals are view row + `_trimmed`, so `trim_history` renumbering cannot move
   a row between chunks; trimming settles the open chunk first, then erases whole
   chunks' keys and compacts the boundary chunk.
4. `_write_file` serializes every rule into
   `Packages/User/vterm/vterm.hidden-color-scheme`, the path the view's `color_scheme`
   setting points at. **Each write makes Sublime reload the scheme and restyle every
   view**, so writes are debounced 250 ms onto the async thread.

Details worth knowing before touching `scheme.py`:

- Rules are **seeded** at build time for all 16 foregrounds (plain and bold) so the very
  first frame never references a scope the file lacks.
- If a rule's resolved background equals the global background *exactly*, it is nudged by
  one (`next_color`) — `add_regions` inverts foreground and background when they match.
- Colours are resolved when a rule is added, so a palette change cannot append: `_build`
  rebuilds the whole table.
- Truecolor SGR is quantized into the 256 palette (`quantize_256`), which bounds the rule
  table: every distinct pair would otherwise be a permanent rule and another file write.
- `plugin_loaded` writes the file **synchronously**, because a restored tab's
  `color_scheme` is read during startup.

### The alternate screen

While an app owns the alternate buffer, `sync_alt_fold` folds rows `0..offset-1` away, so
the view shows the app's screen alone. The fold is re-checked every frame, not just on
transitions, because the screen's first row is rewritten on every region scroll and an
edit at the fold boundary can silently break a fold. Entering the alt screen collapses the
layout, so that transition also zeroes `terminal.viewport_y`; a stale anchor would hold
the follow-scroll gate shut. A fold merely repaired keeps the anchor, or the boundary row
apps rewrite on every scroll would reset it every frame. The emulator side
(`switch_to_screen`) swaps in a `maxlen=0` history, so a full-screen app never pushes its
frames into scrollback.

### Input: a key press reaches the app

Almost every key, **including every printable character**, is bound to `vterm_keypress`.
That is deliberate: if printables were left to Sublime, an app that does not echo (vim in
normal mode) would leave the typed character sitting in the view.

```
keymap binding (context: setting.vterm_view)
  → VtermKeypressCommand.run(key=…, ctrl=…, alt=…, shift=…)
  → view_scrolled()          with scroll_modifier, PgUp/PgDn scroll the view a page
                             and Up/Down a row; stops here, never on the alt screen
  → Terminal.send_key()      adds application_mode (DECCKM), new_line_mode (LNM)
  → keys.get_key_code()      modified nav keys → CSI 1;<mod><final>
                             enter/tab/backspace/space, modified, while the app
                             asked for modifyOtherKeys → CSI 27;<mod>;<code>~
                             ctrl → control byte; alt → ESC prefix
                             plain → _KEY_MAP / _APP_MODE_KEY_MAP
  → Terminal.send_string(normalized=False)
  → pty write                ≤4096 bytes inline, larger goes through a queue
```

**IME and composed input** cannot work this way — there is no key event for "the IME
committed 日本語"; the text arrives as Sublime's own `insert` command. `on_modified`
catches that: it reconstructs what was inserted from the caret positions (not from
`args["characters"]`, which reports the typed keys rather than the committed
composition), forwards it as an unbracketed paste, and `soft_undo`s any other command
that modified the view. Text the emulator did not produce must never remain in the view.

**Copy and paste** are rerouted by `on_text_command`: `copy` → `vterm_copy` (which strips
the zero-width `CONTINUATION` sentinel so soft-wrapped lines re-join), `paste` →
`vterm_paste`, and `undo` → `vterm_noop`. Pastes are wrapped in `ESC[200~` / `ESC[201~`
when the app enabled bracketed paste. In the other direction, OSC 52 from the app reaches
the system clipboard via a hop to the UI thread.

### Mouse

Once per tick, `_sync_wheel_flag` publishes `vterm_view.wheel_captured` = "the app
reports the mouse, or owns the alternate screen". The mousemap's binding is gated on it,
which produces three cases:

1. **App tracks the mouse** → `vterm_scroll` maps the event to a screen cell and sends
   three SGR wheel reports, *one per write* — pi matches the SGR form against a whole
   stdin chunk and drops anything longer.
2. **Alternate screen, no tracking** → three arrow keys per notch (alternate scroll, as
   kitty/iTerm2/xterm do). This is what scrolls a fullscreen UI before it enables mouse
   reporting.
3. **Neither** → the binding does not match at all and Sublime scrolls the view over the
   real scrollback text.

**The trackpad** fires none of that: on macOS it never reaches a mousemap binding
(sublime_text#1838) and dispatches no command, so a plugin cannot see it at all. Its only
in-API trace is the viewport moving, and reading a gesture that way means letting the view
move before pinning it back — visibly, at every poll. vterm does not try: the trackpad is
converted outside the plugin, by a utility such as LinearMouse set to discrete scrolling
for Sublime Text, and arrives as ordinary wheel notches on the path above. Nothing in the
plugin is trackpad-aware, and the viewport is never moved to measure input.

Clicks: `VtermMouseEventListener` intercepts `drag_select` only when the app tracks the
mouse, and only the plain single-button form, so shift-click and add-selection stay
native. The first click on an unfocused tab only focuses it — we replaced the command
that normally does that — and subsequent clicks send a press/release pair.

### Lifecycle

- **Tab closed** → `on_pre_close` → `Terminal.kill()`: unsubscribe, terminate the child,
  drop the registry entry.
- **Process exits** → reader gets `EOFError`, sets `alive = False`; the renderer drains
  the last output, then posts `vterm_cleanup` to the UI thread. Cleanup latches
  `vterm_view.finished`, renders a final frame, kills the terminal, and either closes the
  tab (`auto_close`) or appends `[process exited with code N]`.
- **Plugin host restart** → the registry is gone but the view survives with its settings
  and text. `on_activated` detects a view marked `vterm_view` with no terminal behind it
  and shows a "disconnected" status: the tab has to be closed and reopened.

## Threading

| Thread | Runs | Touches |
|---|---|---|
| UI | commands, listeners, `Terminal.start`, `scroll_to_cursor`, clipboard | Sublime API; small pty writes go out here too |
| reader | `process.read`, decode, `chunks.append` | no Sublime API, no screen state |
| renderer | `stream.feed`, then `run_command("vterm_render")` | owns the emulator |
| paste drain | transient; empties the 4096-byte chunk queue | pty only |
| ST async worker | `SchemeManager._flush` | the scheme file |

The emulator needs no lock because exactly one thread mutates it. The chunk deque needs
none either: `append` and `popleft` are atomic under the GIL. This matters — the renderer
used to hold a lock across `stream.feed` **and** the whole render command, so the reader
could not drain the pty for the duration of a frame and the child blocked in `write()`.
Backpressure now lives in `FEED_LIMIT` instead: at most 256 KiB is parsed per tick, so a
huge burst still renders intermediate frames while the reader keeps draining.

`SchemeManager` does keep a lock, because `scope()` is called from the renderer while
`reload()` and `_flush()` run on other threads. Its read fast path skips the lock; the
invariant that makes that safe is that `_names` must never outlive `rules` — `_build`
replaces both together.

## Sublime settings

**Plugin settings** (`vterm.sublime-settings`, read through the cached `settings()`
handle): `auto_close`, `scrollback_history_size`, `honor_erase_scrollback`,
`scroll_modifier`, `min_columns`, `max_columns`, `theme`, `ansi_colors`, `terminal`,
`parameters`, `debug`. `scroll_modifier` is the modifier that turns PgUp/PgDn and Up/Down
into a scroll of the view — a page, a row — instead of a key for the app. `terminal` and
`parameters` name the external terminal spawned by *vterm: Open Terminal in Project
Folder* (`%CWD%` in `parameters` becomes the project folder). ED 3 never touches the visible screen (as in
xterm), and `honor_erase_scrollback` is off, so it does not drop the scrollback either:
pi and Claude Code repaint by erasing
saved lines, which would take the history with them (`cmd+shift+k` still empties the tab). Changing `theme` fires `SchemeManager.reload`, which rebuilds the
palette and restyles open terminals in place.

**Stamped on every terminal view** by `vterm_initialize_view`: `vterm_view` (the marker
every context and hook keys off), `is_widget`, `color_scheme` (the generated scheme),
`syntax` (the empty one), `scroll_past_end`, `scroll_context_lines`, and a list of
disables — `gutter`, `highlight_line`, `word_wrap`, `auto_complete`, `draw_white_space`,
`draw_indent_guides`, `spell_check`, `translate_tabs_to_spaces`, `command_mode` (which
defuses Vintage mode inside a terminal). The view is also set scratch, so closing it never
prompts to save.

**Written at runtime**: `vterm_view.finished` (latched by cleanup; also disables the
Escape binding for a dead tab), `vterm_view.wheel_captured` (the mousemap gate), and
`block_caret` / `caret_style` (from DECSCUSR; style 0 is the terminal default, a blinking block).

**Contexts.** Keymap bindings use the native `setting.vterm_view`, evaluated in
Sublime's own process — a plugin `on_query_context` would cost a round trip on every
keystroke in every view. Mousemaps do **not** evaluate `setting.` contexts the same way,
so the single wheel context still goes through `on_query_context`, which answers that one
key and returns `None` for everything else.

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
  foreground/background, `OSC 52` reads or writes the system clipboard.
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
