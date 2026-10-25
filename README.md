# vterm

Minimalist terminal for Sublime Text 4, built to run TUI coding agents
(`pi`, `claude`) in a tab. macOS only. No panels, no images,
no build-system integration.

Requires Sublime Text 4 with the Python 3.8 plugin host (build 4149+, for
`View.folded_regions`) and Package Control.

## Install

### With Package Control

vterm is distributed through the shared channel at
[vaclavt/sublime-chanel](https://github.com/vaclavt/sublime-chanel), not the
default one. Open **Preferences → Package Settings → Package Control →
Settings** and put the channel URL in the `channels` array of the right-hand
(User) pane:

```json
{
    "channels": [
        "https://packagecontrol.io/channel_v3.json",
        "https://raw.githubusercontent.com/vaclavt/sublime-chanel/main/channel.json"
    ]
}
```

Keep `https://packagecontrol.io/channel_v3.json` in the list — dropping it
hides every default package. Then run **Package Control: Install Package**
from the Command Palette and pick **vterm**. Upgrades arrive through
**Package Control: Upgrade Package** like any other package.

### From source

Clone the repository into the Sublime Text `Packages` directory as `vterm`:

```sh
git clone https://github.com/vaclavt/vterm.git \
  "$HOME/Library/Application Support/Sublime Text/Packages/vterm"
```

Use one method or the other, not both: an unpacked `Packages/vterm` directory
overrides the `vterm.sublime-package` that Package Control installs.

Either way, dependencies (pyte, ptyprocess, wcwidth) are Package Control
libraries declared in `dependencies.json`. If they are not yet in
`Lib/python38` (e.g. Terminus was never installed), run **Package Control:
Satisfy Libraries** and restart Sublime. Package Control garbage-collects
libraries no package requires, so if imports ever fail after uninstalling
another package, re-run *Satisfy Libraries*.

## Use

Command palette:

- **vterm: Open Terminal** — login shell in a tab (cwd = first project folder,
  else the active file's directory, else `~`)
- **vterm: Open Terminal in Project Folder** — opens the external terminal from
  the settings (Apple Terminal by default) in the project folder of the active file
- **vterm: Clear Screen** (`cmd+shift+k`) — empties the tab, its screen and all
  scrollback, then asks the app to redraw, so the tab looks freshly started
- **vterm: Select Theme** — pick a colour palette, previewing each as you move
  through the list; Esc restores what you had

To launch an agent directly, add your own palette entry or keybinding — `vterm_open`
takes `cmd`, `cwd`, `env` and `title`:

```json
{ "caption": "vterm: pi", "command": "vterm_open", "args": {"cmd": ["pi"], "title": "pi"} }
```

Keys: Shift+Enter sends `ESC CR`, or `CSI 27;2;13~` while the app has xterm
modifyOtherKeys on — pi turns it on and reads a bare `ESC CR` as Alt+Enter,
which submits instead of breaking the line. Shift+PgUp/PgDn page
the view over the scrollback and Shift+Up/Down move it one line, instead of
reaching the app, as xterm and kitty do — `scroll_modifier` moves that to Ctrl,
or turns it off. Copy/paste use the normal Sublime keys, and pastes are
bracketed when the app asks for it. Copying re-joins soft-wrapped lines. The
middle button pastes the current selection straight into the app, X11-style,
without going near the clipboard; it does nothing when nothing is selected. The
wheel scrolls the view normally; while the app tracks the mouse the wheel, clicks
and drags go to it instead (Claude Code's own mouse selection and copy work as in
kitty), and on the alternate screen without tracking each wheel tick sends arrow
keys (alternate scroll, as kitty/iTerm2 do — this is what scrolls Claude Code's
fullscreen UI before it enables mouse reporting).
Sublime raises no wheel event at all for the trackpad (sublime_text#1838): set
a utility such as [LinearMouse](https://linearmouse.app/) to discrete scrolling
for Sublime Text and it arrives as wheel notches like any other. A notch sends the
app one scroll line; `wheel_lines` raises that (iTerm2 sends 3) when the wheel
feels slow. While an app owns the
alternate screen the scrollback above it is folded away, kitty-style, and
unfolds when the app leaves it.

## Settings

`vterm.sublime-settings`: `auto_close` (close the tab when the process exits;
off leaves it open with a `[process exited with code N]` line),
`scrollback_history_size`, `scroll_modifier` (the modifier that scrolls the
view with PgUp/PgDn and Up/Down; `null` gives all four keys to the app),
`min_columns` / `max_columns`, `terminal` / `parameters` (the external terminal
for *vterm: Open Terminal in Project Folder*; `%CWD%` in `parameters` is
replaced with the project folder, defaulting to Apple Terminal, with kitty
commented out beside it), `debug` (verbose logging to the ST console), plus the
colours:

- `theme` — `"default"` (Terminal.app Basic, light), `"homebrew"`, `"pro"`,
  `"slate"`, `"red-alert"`, or `null` to follow the active Sublime colour scheme. The
  default for terminals opened from now on. *vterm: Select Theme* retints the
  focused terminal immediately and sets this; other already-open terminals keep
  whatever theme they had.
- `ansi_colors` — override individual base colours, e.g. `{"red": "#ff0000"}`,
  applied on top of the theme. Takes effect after a Sublime restart.

A light theme is reported to the running agent (OSC 11, DSR 996), so `pi` and
`claude` switch to their light UI to match. An app subscribed to mode 2031 is also
told unprompted when you change the theme mid-session.

## How it works

For the full picture — class inventory, data flows, threading, and a primer on the
terminal protocols — see `Architecture.md`.

- `vterm/emulator.py` — pyte subclasses. The parser is rewritten because stock
  pyte swallows what these agents rely on: `>`/`<`/`=` prefixed CSI, DECRQM `$p`,
  multi-digit OSC. It answers DA1 (Claude Code's query sentinel, which has no
  timeout) and DA2, CPR, XTVERSION + DECRQM 2026 (this is what unlocks Claude
  Code's flicker-free synchronized output on a generic `TERM`), the kitty keyboard
  probe, and OSC 10/11 colour queries; OSC 52 (clipboard write) is ignored,
  since any output could plant a command to paste.
  Combining characters are merged as NFC, because Sublime draws decomposed
  sequences badly.
- `vterm/terminal.py` — pty process, reader and renderer threads at ~33 Hz.
  Rendering is held back inside a `?2026` frame, and the pty is resized from the
  view size (TIOCSWINSZ → SIGWINCH). Reads go through an incremental UTF-8
  decoder so a sequence split across chunks is not dropped. The child gets
  `TERM=xterm-256color` and `COLORTERM=truecolor`, with `TERM_PROGRAM` stripped.
  A forced repaint narrows the pty by one column for 250 ms — never ctrl+L, which
  is `/clear` in Claude Code.
- `vterm/render.py` — scrollback and live screen are real view text: dirty rows
  are whole-line replaced, colours are `add_regions` overlays, and the Sublime
  caret doubles as the terminal cursor (DECTCEM turns it into a block, so an app
  drawing its own lands on the same cell; a view with no caret at all loses the
  space key and IME text).
- `vterm/scheme.py` + `vterm/themes.py` — colour scheme rules generated on demand
  per (fg, bg, style) into `Packages/User/vterm/`, truecolor quantized into the
  256 palette to bound the rule table, palette from the selected theme or your
  editor scheme.
- `vterm/keys.py` + generated `Default.sublime-keymap` — every key including the
  printable characters is bound to `vterm_keypress`, so nothing is ever inserted
  into the view: an app that does not echo (vim in normal mode) would otherwise
  leave the character behind. `on_modified` in `vterm/listeners.py` remains the
  fallback for IME and composed input.
- `vterm/mouse.py` — SGR mouse reporting (wheel, click, drag) when the app enables
  tracking; native selection and scrolling otherwise. The trackpad is not
  handled: it reaches no mousemap on macOS and is converted to wheel notches
  outside the plugin.

Supported: alt screen (1049), synchronized output (2026), bracketed paste (2004),
mouse tracking 1000/1002/1003 + SGR 1006 (wheel and left button are reported; a
drag arrives as one motion at release, since Sublime raises none; no right
button; the middle button pastes the selection instead of being reported),
focus events (1004),
xterm modifyOtherKeys (`CSI > 4 ; n m`, for the modified keys with no
unambiguous legacy encoding: enter, tab, backspace, space), cursor styles (DECSCUSR —
block/bar and blink/steady via the view's caret settings; underline falls back
to bar, 0 is the default blinking block), scroll regions, autowrap,
50 000 lines of scrollback that ED 3 does not erase (agents repaint with it;
`honor_erase_scrollback` restores the usual meaning),
256-colour and truecolor SGR (truecolor quantized to the 256 palette),
wcwidth-aware wide characters, OSC 0/2 titles,
DEC line drawing (`ESC(0`, so ncurses apps such as mutt or dialog draw real
borders even under a non-UTF-8 locale).
Deliberately absent: the kitty keyboard protocol (legacy encodings are sent
instead), image protocols, Windows, panels.

## Rough edges

- Streaming output is expensive: every scroll marks all rows dirty and each row is
  rewritten over the plugin-host IPC boundary. A per-row cache skips rewriting rows
  whose content did not change, but a genuinely changing full screen still costs
  real CPU. Batching the colour regions across a frame is the known next fix.
- Editing `vterm/*.py` needs a **Sublime restart**. Submodules are deliberately
  not purged on reload, because rebuilding the terminal registry silently orphans
  open tabs; an orphaned tab shows a "disconnected" status and stops accepting
  input, so close it and open a new one.
- CJK and emoji rely on wcwidth agreeing with Sublime's font metrics; where they
  disagree the caret can drift.
- Trackpad scrolling needs a helper on macOS: Sublime never delivers it to a
  plugin (sublime_text#1838), so set a utility such as
  [LinearMouse](https://linearmouse.app/) to discrete/linear scrolling for
  Sublime Text. It then arrives as wheel notches and scrolls the app, three
  lines a notch unless `wheel_lines` says otherwise. Without
  one, the trackpad does nothing in a full-screen app; the mouse wheel,
  `shift+pageup/pagedown` and `shift+up/down` work regardless.

## Dev

```sh
python3 tests/test_emulator.py   # emulator, query responder, clear — chunked and char-by-char
python3 tests/test_imports.py    # modules import, keymap parses, command/flag/theme wiring
python3 tests/test_render.py     # render layer on a fake view: trimming, selection, colouring
python3 tests/test_commands.py   # theme picker, live restyle, generated scheme
python3 tests/harness_live.py CMD SECONDS [--send TEXT]   # run a real app in the emulator, dump the screen
python3 tools/gen_keymap.py      # regenerate Default.sublime-keymap
python3 benchmarks/bench_vs_terminus.py   # render cost per frame, against Terminus on the same input
python3 benchmarks/profiling.py           # feed-path hotspots, per-char micro-benches of draw
```

`harness_live.py` is the useful one: it drives a real `pi` or `claude` through the
emulator with no Sublime involved, and prints the queries answered plus the
resulting screen. Tests import pyte/ptyprocess/wcwidth from Sublime's
`Lib/python3*` (pure Python, so any Python 3 runs them).

Code style is in `CODE.md`, working agreements in `CLAUDE.md`.
