# vterm

Minimalist terminal for Sublime Text 4, built to run TUI coding agents
(`pi`, `claude`) in a tab. macOS only. No panels, no images,
no build-system integration.

Requires Sublime Text 4 with the Python 3.8 plugin host (build 4149+, for
`View.folded_regions`) and Package Control.

## Install

Symlink this folder into Sublime packages as `vterm`:

```sh
ln -s /Users/vaclavt/devel/vt/vterm "$HOME/Library/Application Support/Sublime Text/Packages/vterm"
```

Dependencies (pyte, ptyprocess, wcwidth) are Package Control libraries declared
in `dependencies.json`. If they are not yet in `Lib/python38` (e.g. Terminus was
never installed), run **Package Control: Satisfy Libraries** and restart Sublime.
Package Control garbage-collects libraries no package requires, so if imports
ever fail after uninstalling another package, re-run *Satisfy Libraries*.

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

Keys: Shift+Enter sends `ESC CR` (newline in both agents). Shift+PgUp/PgDn page
the view over the scrollback and Shift+Up/Down move it one line, instead of
reaching the app, as xterm and kitty do — `scroll_modifier` moves that to Ctrl,
or turns it off. Copy/paste use the normal Sublime keys, and pastes are
bracketed when the app asks for it. Copying re-joins soft-wrapped lines. The
wheel scrolls the view normally; while the app tracks the mouse the wheel and
left click go to it instead, and on the alternate screen without tracking each
wheel tick sends arrow keys (alternate scroll, as kitty/iTerm2 do — this is
what scrolls Claude Code's fullscreen UI before it enables mouse reporting).
Sublime raises no wheel event at all for the trackpad (sublime_text#1838), so
while the app owns the wheel a poller reads the viewport movement instead,
sends it as scroll lines and pins the view back. A jump of a whole screen or
more — the scrollbar, the page keys — is left alone, so the scrollback is still
yours to browse until you come back to the bottom. While an app owns the
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
  `"slate"`, or `null` to follow the active Sublime colour scheme. Set it with
  *vterm: Select Theme* or by hand; either way open terminals restyle in place,
  no restart needed.
- `ansi_colors` — override individual base colours, e.g. `{"red": "#ff0000"}`,
  applied on top of the theme.

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
  probe, and OSC 10/11 colour queries; OSC 52 goes to the Sublime clipboard.
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
- `vterm/mouse.py` — SGR mouse reporting (wheel, click) when the app enables
  tracking; native selection and scrolling otherwise. The trackpad reaches no
  mousemap on macOS, so while the app owns the wheel a 24 ms poller harvests
  viewport displacement into the same reports.

Supported: alt screen (1049), synchronized output (2026), bracketed paste (2004),
mouse tracking 1000/1002/1003 + SGR 1006 (wheel and left click are reported; no
drag, no right or middle button), focus events (1004), cursor styles (DECSCUSR —
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
- Trackpad scroll into an app is polled and rounded to whole lines, so it is
  coarser than the wheel in a native terminal.

## Dev

```sh
python3 tests/test_emulator.py   # emulator, query responder, clear — chunked and char-by-char
python3 tests/test_imports.py    # modules import, keymap parses, command/flag/theme wiring
python3 tests/test_render.py     # render layer on a fake view: trimming, selection, colouring
python3 tests/test_commands.py   # theme picker, live restyle, generated scheme
python3 tests/harness_live.py CMD SECONDS [--send TEXT]   # run a real app in the emulator, dump the screen
python3 tools/gen_keymap.py      # regenerate Default.sublime-keymap
```

`harness_live.py` is the useful one: it drives a real `pi` or `claude` through the
emulator with no Sublime involved, and prints the queries answered plus the
resulting screen. Tests import pyte/ptyprocess/wcwidth from Sublime's
`Lib/python38` (pure Python, so any Python 3 runs them).

Code style is in `CODE.md`, working agreements in `CLAUDE.md`.
