"""Claude Team Helper for Windows.

The same two tools as the macOS app:
  * Sidebar Sync - copy Claude desktop's Code-tab sessions into the account in use.
  * Computer Control - install the pyautogui MCP server (computer_control.py) and
    register it with Claude Code and, optionally, Claude chat.

`ClaudeTeamHelper.exe --mcp` runs the MCP server itself, so nothing else has to be
installed on the PC. Built on macOS with py2bin (see build_windows.sh).
"""
from __future__ import annotations

import sys

if "--mcp" in sys.argv:  # MCP server mode: no GUI, stdio only
    import computer_control

    computer_control.main([])
    sys.exit(0)

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import webview

import sync_engine as se

APP_NAME = "Claude Team Helper"
SERVER = "computer-control"
HOME = Path.home()
APPDATA = Path(os.environ.get("APPDATA", HOME / "AppData" / "Roaming"))
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData" / "Local"))
CLAUDE_DIR = APPDATA / "Claude"
SESSIONS = CLAUDE_DIR / "claude-code-sessions"
DESKTOP_CONFIG = CLAUDE_DIR / "claude_desktop_config.json"
CLAUDE_JSON = HOME / ".claude.json"
BACKUPS = LOCALAPPDATA / "ClaudeTeamHelper" / "backups"
INSTALL_DIR = LOCALAPPDATA / "Programs" / "ClaudeTeamHelper"
SHORTCUT = APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flashes from a windowed app
KEEP_BACKUPS = 5


def app_dir() -> Path:
    """The frozen bundle's root folder (the exe itself sits in its runtime\\ folder)."""
    root = os.environ.get("PY2BIN_BUNDLE_ROOT")
    return Path(root).resolve() if root else Path(sys.executable).resolve().parent


def exe_rel() -> Path:
    """Where the GUI exe sits inside the bundle, e.g. runtime\\ClaudeTeamHelper.exe."""
    try:
        return Path(sys.executable).resolve().relative_to(app_dir())
    except ValueError:
        return Path(Path(sys.executable).name)


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    if cmd and cmd[0].lower().endswith((".cmd", ".bat")):
        cmd = ["cmd", "/c", *cmd]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          creationflags=NO_WINDOW if os.name == "nt" else 0)


def powershell(script: str, timeout: int = 60) -> str:
    r = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout)
    return r.stdout.strip()


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


def human(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


# ---------------------------------------------------------------------------
# Claude desktop app
# ---------------------------------------------------------------------------

# Desktop-app processes are claude.exe outside the Claude Code CLI's own folders.
_PS_CLAUDE = (r"Get-Process -Name claude -ErrorAction SilentlyContinue | Where-Object { $_.Path -and "
              r"$_.Path -notmatch '\\\.local\\bin\\|\\claude-code\\|\\npm\\' }")


def claude_processes() -> list[tuple[int, str, bool]]:
    out = powershell(_PS_CLAUDE + " | ForEach-Object { \"$($_.Id)|$($_.Path)|$($_.MainWindowHandle -ne 0)\" }")
    procs = []
    for line in out.splitlines():
        pid, _, rest = line.partition("|")
        path, _, has_window = rest.rpartition("|")
        if pid.isdigit():
            procs.append((int(pid), path, has_window == "True"))
    return procs


def claude_launch_path(procs: list[tuple[int, str, bool]]) -> str | None:
    for _, path, _ in procs:
        p = Path(path)
        # Squirrel installs run app-<version>\claude.exe; the stub one level up picks the newest.
        if p.parent.name.startswith("app-") and (p.parent.parent / "claude.exe").exists():
            return str(p.parent.parent / "claude.exe")
        return path
    stub = LOCALAPPDATA / "AnthropicClaude" / "claude.exe"
    return str(stub) if stub.exists() else None


def quit_claude(force: bool, say) -> bool:
    procs = claude_processes()
    if not procs:
        return True
    if force:
        say("Force-quitting Claude…")
        powershell(_PS_CLAUDE + " | Stop-Process -Force")
    else:
        say("Asking Claude to close… (confirm in Claude if it asks)")
        powershell(_PS_CLAUDE + " | ForEach-Object { $_.CloseMainWindow() | Out-Null }")
    for _ in range(40 if not force else 20):
        time.sleep(0.5)
        if not claude_processes():
            return True
    return False


def open_claude(path: str | None, say) -> None:
    if path and Path(path).exists():
        subprocess.Popen([path], creationflags=0x00000008 | NO_WINDOW, close_fds=True)  # DETACHED_PROCESS
        say("Reopened Claude.")
    else:
        say("Open Claude again from the Start menu.")


# ---------------------------------------------------------------------------
# Account Sync (the engine lives in sync_engine.py, shared with the tests)
# ---------------------------------------------------------------------------

STORE = se.Store(CLAUDE_DIR)
NAMES = LOCALAPPDATA / "ClaudeTeamHelper" / "account-names.json"


def folders_with_names() -> list[dict]:
    folders = se.scan(STORE)
    names = se.learn_names(STORE, CLAUDE_JSON, NAMES, folders)
    for f in folders:
        f["title"], f["detail"] = se.label(f, names, folders)
    return folders


# ---------------------------------------------------------------------------
# Computer Control MCP
# ---------------------------------------------------------------------------

def claude_cli() -> str | None:
    for c in (shutil.which("claude"), HOME / ".local" / "bin" / "claude.exe", APPDATA / "npm" / "claude.cmd"):
        if c and Path(c).exists():
            return str(c)
    return None


def server_command(root: Path) -> list[str]:
    """How Claude should start the MCP server from an installed copy at `root`.

    The bundle's console python.exe gives the server real stdio pipes; the bootstrap it shares
    with the windowed exe runs app.py, which sees --mcp and serves instead of opening a window.
    """
    console = root / exe_rel().parent / "python.exe"
    if console.exists():
        return [str(console), str(root / "app" / "app.py"), "--mcp"]
    return [str(root / exe_rel()), "--mcp"]


def registered_for_code() -> bool:
    return SERVER in (read_json(CLAUDE_JSON).get("mcpServers") or {})


def registered_for_chat() -> bool:
    return SERVER in (read_json(DESKTOP_CONFIG).get("mcpServers") or {})


def set_chat_registration(on: bool) -> None:
    cfg = read_json(DESKTOP_CONFIG)
    servers = cfg.setdefault("mcpServers", {})
    if on:
        cmd = server_command(INSTALL_DIR)
        servers[SERVER] = {"command": cmd[0], "args": cmd[1:]}
    else:
        servers.pop(SERVER, None)
    DESKTOP_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = DESKTOP_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(DESKTOP_CONFIG)


def make_shortcut(target: Path) -> None:
    SHORTCUT.parent.mkdir(parents=True, exist_ok=True)
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{SHORTCUT}');"
          f"$s.TargetPath='{target}';$s.WorkingDirectory='{target.parent}';$s.Save()")
    powershell(ps)


# ---------------------------------------------------------------------------
# JavaScript bridge
# ---------------------------------------------------------------------------

class Api:
    def __init__(self) -> None:
        self.window: webview.Window | None = None
        self._lock = threading.Lock()

    def _say(self, line: str) -> None:
        if self.window:
            self.window.evaluate_js(f"addLog({json.dumps(line)})")

    # -- state -------------------------------------------------------------
    def state(self) -> dict:
        folders = folders_with_names()
        installed = (INSTALL_DIR / exe_rel()).exists()
        return {
            "folders": folders,
            "categories": se.CATEGORIES,
            "claudeFound": CLAUDE_DIR.exists(),
            "installed": installed,
            "size": human(folder_size(INSTALL_DIR)) if installed else "",
            "forCode": registered_for_code(),
            "forChat": registered_for_chat(),
            "cli": bool(claude_cli()),
            "runningFromInstall": app_dir() == INSTALL_DIR.resolve() if INSTALL_DIR.exists() else False,
        }

    def plan(self, src: str, dst: str, both: bool) -> dict:
        p = se.plan(STORE, src, dst)
        if both:
            r = se.plan(STORE, dst, src)
            p = {"counts": {k: v + r["counts"][k] for k, v in p["counts"].items()}, "titles": p["titles"] + r["titles"]}
        return p

    def rename(self, org: str, name: str) -> None:
        se.set_nickname(NAMES, org, name)

    # -- Account Sync --------------------------------------------------------
    def sync(self, src: str, dst: str, cats: list[str], both: bool, reopen: bool, force: bool = False) -> dict:
        with self._lock:
            if sum(self.plan(src, dst, both)["counts"].get(c, 0) for c in cats) == 0:
                self._say("Nothing to sync.")
                return {"ok": True}
            procs = claude_processes()
            relaunch = claude_launch_path(procs) if procs else None
            if procs and not quit_claude(force, self._say):
                self._say("Claude is still running (it may be in the system tray). Nothing was changed.")
                return {"ok": False, "needsForce": True}
            for a, b in [(src, dst)] + ([(dst, src)] if both else []):
                try:
                    bk = se.backup(STORE, BACKUPS, b, KEEP_BACKUPS)
                    self._say(f"Backed up {b[-36:-28]} → {bk.name}")
                    se.apply(STORE, set(cats), a, b, lambda m: self._say("  " + m))
                except (OSError, shutil.Error) as e:
                    self._say(f"Error: {e}")
            self._say("Done.")
            if relaunch and reopen:
                open_claude(relaunch, self._say)
            return {"ok": True}

    def show_backups(self) -> None:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        os.startfile(BACKUPS)

    # -- Computer Control ----------------------------------------------------
    def install(self, also_chat: bool) -> dict:
        with self._lock:
            src = app_dir()
            try:
                if src != INSTALL_DIR.resolve():
                    self._say(f"Copying the app to {INSTALL_DIR} …")
                    shutil.copytree(src, INSTALL_DIR, dirs_exist_ok=True)
                make_shortcut(INSTALL_DIR / exe_rel())
                self._say("Added a Start menu shortcut.")
            except OSError as e:
                self._say(f"✗ {e}")
                return {"ok": False}
            cmd = server_command(INSTALL_DIR)
            r = run(cmd + ["--check"], timeout=60)
            if r.returncode != 0:
                self._say(f"✗ Server check failed: {(r.stderr or r.stdout).strip()[-400:]}")
                return {"ok": False}
            self._say("Server check: " + r.stdout.strip())
            cli = claude_cli()
            if cli:
                run([cli, "mcp", "remove", "--scope", "user", SERVER])
                r = run([cli, "mcp", "add", "--scope", "user", SERVER, "--", *cmd])
                self._say(("✓ " if r.returncode == 0 else "✗ ") + "Registered with Claude Code"
                          + ("" if r.returncode == 0 else f": {r.stderr.strip()}"))
            else:
                self._say("! Claude Code CLI not found, so the Code tab was not registered. "
                          "Install Claude Code, then press Install again.")
            if also_chat:
                self._chat(True)
            self._say("✓ Done. Start a new Claude session to use the computer-control tools.")
            return {"ok": True}

    def _chat(self, on: bool) -> None:
        procs = claude_processes()
        relaunch = claude_launch_path(procs) if procs else None
        if procs and not quit_claude(False, self._say):
            self._say("✗ Claude did not quit; chat config left unchanged.")
            return
        try:
            set_chat_registration(on)
            self._say("✓ " + ("Added to" if on else "Removed from") + " Claude chat config.")
        except OSError as e:
            self._say(f"✗ Chat config: {e}")
        if relaunch:
            open_claude(relaunch, self._say)

    def uninstall(self) -> dict:
        with self._lock:
            cli = claude_cli()
            if cli and registered_for_code():
                run([cli, "mcp", "remove", "--scope", "user", SERVER])
                self._say("Removed from Claude Code.")
            if registered_for_chat():
                self._chat(False)
            SHORTCUT.unlink(missing_ok=True)
            if app_dir() == INSTALL_DIR.resolve():
                # Running from the installed copy: delete it once this window closes.
                subprocess.Popen(["cmd", "/c", f'timeout /t 3 >nul & rmdir /s /q "{INSTALL_DIR}"'],
                                 creationflags=NO_WINDOW | 0x00000008, close_fds=True)
                self._say("The installed copy will be deleted when you close this window.")
            else:
                shutil.rmtree(INSTALL_DIR, ignore_errors=True)
                self._say("✓ Removed the installed copy.")
            return {"ok": True}

    def test(self) -> None:
        r = run(server_command(INSTALL_DIR) + ["--self-test"], timeout=60)
        for line in (r.stdout or r.stderr).strip().splitlines():
            self._say(line)

    def copy_prompt(self) -> None:
        import computer_control as cc  # reuse its clipboard code
        cc.get_backend().clipboard_set("Use the computer-control tools: run check_permissions, take a "
                                       "screenshot, and tell me what apps are open.")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<style>
:root{--bg:#f6f6f8;--card:#fff;--fg:#1d1d1f;--muted:#6e6e73;--line:#e3e3e8;--accent:#3b6ff5;--ok:#1aa34a;--chip:#e8eefe}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--fg:#f2f2f7;--muted:#a1a1a6;--line:#3a3a3d;--accent:#5b8cff;--chip:#2c3550}}
*{box-sizing:border-box}body{margin:0;font:14px "Segoe UI Variable","Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--fg)}
.wrap{padding:22px 24px}header{display:flex;gap:14px;align-items:center;margin-bottom:16px}
.logo{width:48px;height:48px;border-radius:12px;background:linear-gradient(150deg,#d9734d,#5c40b3);display:grid;place-items:center}
h1{font-size:20px;margin:0}.sub{color:var(--muted);margin-top:2px}
.tabs{display:inline-flex;background:var(--line);border-radius:9px;padding:3px;margin-bottom:16px}
.tabs button{border:0;background:none;padding:6px 16px;border-radius:7px;font:inherit;color:var(--fg);cursor:pointer}
.tabs button.on{background:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:6px 14px;margin:8px 0 14px}
.row{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}.row:last-child{border:0}
.mono{font-family:"Cascadia Mono",Consolas,monospace}.muted{color:var(--muted)}.small{font-size:12px}.grow{flex:1}
.badge{background:var(--chip);color:var(--accent);font-size:11px;font-weight:600;padding:1px 7px;border-radius:9px;margin-left:6px}
label.f{display:flex;gap:10px;align-items:center;margin:6px 0}select{font:inherit;padding:5px 8px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);flex:1;min-width:0}
.title{font-weight:600}.pen{border:0;background:none;color:var(--muted);cursor:pointer;font-size:13px}.n{font-variant-numeric:tabular-nums}.cat{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line)}.cat:last-child{border:0}
.bar{display:flex;align-items:center;gap:12px;margin:14px 0}.btn{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);padding:6px 14px;border-radius:8px;cursor:pointer}
.primary{background:var(--accent);border-color:var(--accent);color:#fff;padding:9px 22px;font-weight:600}.btn:disabled{opacity:.5;cursor:default}
.dot{width:16px;height:16px;border-radius:50%;border:2px dashed var(--muted)}.dot.ok{border:0;background:var(--ok)}
#log{white-space:pre-wrap;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;height:110px;overflow:auto;font:12px "Cascadia Mono",Consolas,monospace;display:none}
details{margin:6px 0}summary{cursor:pointer}ul{margin:6px 0;padding-left:20px;max-height:110px;overflow:auto}
</style></head><body><div class="wrap">
<header><div class="logo"><svg viewBox="0 0 48 48" width="34" height="34" fill="none" stroke="#fff" stroke-linecap="round" stroke-width="3.2"><rect x="6" y="9" width="11" height="30" rx="3" fill="#fff" fill-opacity=".25" stroke="none"/><path d="M9 15h5M9 21h5M9 27h5" stroke-width="2.6"/><path d="M24 22a9 9 0 0 1 15.5-5M39.5 11v6h-6M41 28a9 9 0 0 1-15.5 5M25.5 39v-6h6"/></svg></div><div><h1>Claude Team Helper</h1><div class="sub">Move to a Claude Team account without losing your setup.</div></div></header>
<div class="tabs"><button id="t0" class="on" onclick="tab(0)">Account Sync</button><button id="t1" onclick="tab(1)">Computer Control</button></div>
<section id="p0">
 <div class="muted">Copy everything Claude keeps per account on this PC into the account you're using.</div>
 <div class="card" id="folders"></div>
 <div class="bar" style="margin:4px 0"><span class="muted">From</span><select id="src" onchange="replan()"></select>
  <span class="muted" id="arrow">→</span><select id="dst" onchange="replan()"></select></div>
 <div class="muted small" style="margin-top:12px">What gets synced</div>
 <div class="card" id="cats"></div>
 <div class="bar"><div class="grow"><b id="count"></b><br>
   <label class="small"><input type="checkbox" id="both" onchange="replan()"> Both ways (each account gets everything)</label><br>
   <label class="small"><input type="checkbox" id="reopen" checked> Quit and reopen Claude automatically</label></div>
  <button class="btn primary" id="syncBtn" onclick="doSync(false)">Sync Everything</button></div>
 <details id="det"><summary id="detsum">Show new sessions</summary><ul id="list"></ul></details>
 <div class="bar small muted"><span class="grow">Already shared by every account: pins, Claude Code memory, CLAUDE.md, settings and /stats. Each sync backs up first (last 5 kept).</span><button class="btn small" onclick="pywebview.api.show_backups()">Show Backups</button></div>
</section>
<section id="p1" style="display:none">
 <div class="muted">Team plans don't include Anthropic's built-in computer use yet. This installs a pyautogui MCP server so Claude can see the screen and use the mouse and keyboard.</div>
 <div class="card">
  <div class="row"><div class="dot" id="d0"></div><span class="grow">Server installed</span><span class="muted" id="s0"></span></div>
  <div class="row"><div class="dot" id="d1"></div><span class="grow">Available in Claude Code (Code tab, terminal)</span><span class="muted" id="s1"></span></div>
  <div class="row"><div class="dot" id="d2"></div><span class="grow">Available in Claude chat</span><span class="muted" id="s2"></span></div>
 </div>
 <div class="bar"><label class="grow small"><input type="checkbox" id="chat"> Also add to Claude chat (quits and reopens Claude)</label>
  <button class="btn" id="testBtn" onclick="call('test')">Test</button><button class="btn" id="unBtn" onclick="call('uninstall')">Uninstall</button>
  <button class="btn primary" id="inBtn" onclick="call('install', document.getElementById('chat').checked)">Install</button></div>
 <div class="card small"><div class="row"><span class="grow">Windows needs no permission. To control apps running as administrator, run Claude as administrator too.</span>
  <button class="btn small" onclick="pywebview.api.copy_prompt()">Copy test prompt</button></div>
  <div class="row muted">Emergency stop: move the mouse into any screen corner.</div></div>
</section>
<div id="log"></div>
</div><script>
let S={folders:[]},busy=false;
function tab(i){[0,1].forEach(j=>{document.getElementById('p'+j).style.display=i==j?'':'none';document.getElementById('t'+j).className=i==j?'on':''})}
function addLog(s){const l=document.getElementById('log');l.style.display='block';l.textContent+=s+'\n';l.scrollTop=l.scrollHeight}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function ago(t){if(!t)return'No activity';const m=(Date.now()/1000-t)/60;return'Last active '+(m<1?'just now':m<60?Math.round(m)+' min ago':m<1440?Math.round(m/60)+' h ago':Math.round(m/1440)+' days ago')}
async function load(){
 S=await pywebview.api.state();
 const f=document.getElementById('folders');
 f.innerHTML=S.folders.length?S.folders.map((x,i)=>`<div class="row"><div class="grow"><span class="title">${esc(x.title)}</span> <button class="pen" title="Name this account" onclick="rename('${x.org}','${esc(x.title).replace(/'/g,"")}')">✎</button>${i==0?'<span class="badge">In use now</span>':''}<div class="small muted">${esc(x.detail)}</div><div class="small muted" style="opacity:.75">${ago(x.latest)}</div></div><span>${x.count} sessions</span></div>`).join(''):`<div class="row muted">${S.claudeFound?'No Claude session folders found.':'Claude desktop data not found on this PC.'}</div>`;
 const opts=S.folders.map(x=>`<option value="${esc(x.id)}">${esc(x.title)} (${x.org.slice(0,8)})</option>`).join('');
 const src=document.getElementById('src'),dst=document.getElementById('dst'),ps=src.value,pd=dst.value;
 src.innerHTML=opts;dst.innerHTML=opts;
 dst.value=pd&&S.folders.some(x=>x.id==pd)?pd:(S.folders[0]||{}).id||'';
 src.value=ps&&S.folders.some(x=>x.id==ps)?ps:(S.folders.find(x=>x.id!=dst.value)||{}).id||'';
 if(!document.getElementById('cats').childElementCount)
  document.getElementById('cats').innerHTML=S.categories.map(([k,t])=>`<label class="cat"><input type="checkbox" class="c" value="${k}" checked onchange="replan()"><span class="grow">${t}</span><span class="n muted" id="n_${k}"></span></label>`).join('');
 set('d0','s0',S.installed,S.installed?S.size:'not installed');
 set('d1','s1',S.forCode,S.forCode?'registered':(S.cli?'not registered':'Claude Code CLI not found'));
 set('d2','s2',S.forChat,S.forChat?'registered':'optional');
 document.getElementById('inBtn').textContent=S.installed?'Reinstall / Update':'Install';
 ['testBtn','unBtn'].forEach(id=>document.getElementById(id).style.display=S.installed?'':'none');
 replan();
}
async function rename(org,cur){
 const n=prompt('Name this account (leave empty to use the detected name):',cur=='Unnamed organization'?'':cur);
 if(n===null)return;await pywebview.api.rename(org,n);load();
}
function cats(){return [...document.querySelectorAll('.c')].filter(c=>c.checked).map(c=>c.value)}
let P={counts:{},titles:[]};
async function replan(){
 const src=document.getElementById('src').value,dst=document.getElementById('dst').value,both=document.getElementById('both').checked;
 document.getElementById('arrow').textContent=both?'⇄':'→';
 P=src&&dst&&src!=dst?await pywebview.api.plan(src,dst,both):{counts:{},titles:[]};
 for(const [k] of S.categories){const n=P.counts[k]||0,e=document.getElementById('n_'+k);e.textContent=n?n+' new':'up to date';e.style.color=n?'var(--fg)':''}
 const sel=cats(),pending=sel.reduce((a,k)=>a+(P.counts[k]||0),0);
 document.getElementById('count').textContent=pending?`${pending} item${pending==1?'':'s'} to sync`:'Everything is already in sync.';
 document.getElementById('syncBtn').textContent=busy?'Syncing…':(sel.length==S.categories.length?'Sync Everything':'Sync Selected');
 document.getElementById('syncBtn').disabled=busy||!pending;
 document.getElementById('list').innerHTML=P.titles.map(t=>`<li>${esc(t)}</li>`).join('');
 document.getElementById('detsum').textContent=`Show new sessions (${P.titles.length})`;
 document.getElementById('det').style.display=P.titles.length?'':'none';
}
function set(d,s,ok,t){document.getElementById(d).className='dot'+(ok?' ok':'');document.getElementById(s).textContent=t}
async function doSync(force){
 busy=true;replan();
 const r=await pywebview.api.sync(document.getElementById('src').value,document.getElementById('dst').value,cats(),
   document.getElementById('both').checked,document.getElementById('reopen').checked,force);
 busy=false;
 if(r&&r.needsForce&&confirm('Claude did not close (it may be in the system tray).\n\nForce-quit Claude and continue?'))return doSync(true);
 load();
}
async function call(name,...a){
 if(busy)return;busy=true;document.querySelectorAll('#p1 .btn').forEach(b=>b.disabled=true);
 try{await pywebview.api[name](...a)}finally{busy=false;document.querySelectorAll('#p1 .btn').forEach(b=>b.disabled=false);load()}
}
window.addEventListener('pywebviewready',load);
</script></body></html>"""


def main() -> None:
    api = Api()
    api.window = webview.create_window(APP_NAME, html=HTML, js_api=api, width=700, height=900,
                                       min_size=(600, 620), background_color="#f6f6f8")
    webview.start(private_mode=True)


if __name__ == "__main__":
    main()
