# Claude Team Helper

A small macOS and Windows app for moving from a personal Claude account to a Claude Team
account without losing your setup.

| | macOS | Windows |
|---|---|---|
| App | native SwiftUI, 1.3 MB | one 12 MB `ClaudeTeamHelper.exe` (pywebview, built with py2bin) |
| Computer Control install | 15 MB venv (no download cache left behind) | nothing extra: hard links to the app's own files |

## Account Sync

Claude desktop keeps some data per account/org on disk. **Sync Everything** copies all of it
from one account into another (or both ways), only adding what is missing:

- Code sessions (the Code-tab sidebar) and the archived list
- Code routines (scheduled tasks) and custom sidebar groups
- Cowork sessions with their files, Cowork scheduled tasks and settings

Each account shows its name and email ("Personal · Alex · you@example.com"). Names are learned
from what Claude stores locally and remembered after you switch away; ✎ sets your own.

Before every sync the destination is backed up (newest 5 kept) to
`~/Library/Application Support/ClaudeTeamHelper/backups` (Windows:
`%LOCALAPPDATA%\ClaudeTeamHelper\backups`). On APFS the copies are clones, so they take no
extra space. Claude is quit first and reopened afterwards, because it rewrites these files.

Already shared by every account, so nothing to sync: pins, Claude Code memory, CLAUDE.md,
settings and `/stats` (all in `~/.claude`). Custom skills and connectors live on Anthropic's
servers and are not on disk.

## Computer Control (MCP)

Anthropic's built-in computer use is not available on Team plans. **Install** registers
`mcp/computer_control.py`, a pyautogui MCP server, with Claude Code (`claude mcp add --scope
user computer-control`) and optionally Claude chat.

One file serves both platforms and speaks MCP itself (no `mcp` SDK). macOS screenshots are
captured and scaled in-process with Quartz (~85 ms at 4K); Windows uses Pillow's ImageGrab.

Tools: `screenshot`, `zoom`, `click`, `move_mouse`, `drag`, `scroll`, `type_text` (Unicode via
clipboard paste, clipboard restored), `press_keys`, `hold_key`, `mouse_button`, `batch`,
`cursor_position`, `list_displays`, `list_applications`, `open_application`, `list_windows`,
`clipboard_get`, `clipboard_set`, `wait`, `check_permissions`.

- Coordinates are always pixels in the latest screenshot; Retina/HiDPI scaling and multiple
  displays are handled by the server.
- macOS: allow **Claude** under Privacy & Security → Accessibility and Screen Recording.
- **Emergency stop:** move the mouse into any screen corner.

## Build

```bash
./build.sh            # macOS → build/Claude Team Helper.app
./build_windows.sh    # Windows, on this Mac with py2bin → dist/ClaudeTeamHelper.exe
```

`build_windows.sh` resolves Windows wheels with uv, freezes `windows/app.py` with py2bin
(`PY2BIN_SRC` can point at a python_to_binary checkout), drops Pillow codecs the app never
loads, and packs it all into one self-extracting exe that carries its own Python.

On the PC, just run `ClaudeTeamHelper.exe`. The first start unpacks it once into
`%LOCALAPPDATA%\py2bin` (a few seconds); later starts reuse that. **Install** hard-links the
unpacked files into `%LOCALAPPDATA%\Programs\ClaudeTeamHelper` (no second copy on disk),
adds a Start menu shortcut and registers the MCP server. Unpacked copies of older versions
are removed automatically, so updates don't pile up.

## Tests

```bash
tests/run_sync_test.sh    # both sync engines on an APFS clone of your real Claude data
uv venv --python 3.12 .venv && uv pip install --no-deps --python .venv/bin/python -r mcp/requirements-mac.txt
.venv/bin/python tests/mcp_smoke.py --python .venv/bin/python   # MCP over stdio, read-only
```
