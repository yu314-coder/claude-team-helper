"""Smoke test: start the server over stdio and exercise it like an MCP client would.

The only input it sends is moving the pointer to where it already is.
Usage: python tests/mcp_smoke.py [--python PATH] [--save screenshot.jpg]
"""
import base64, json, subprocess, sys, time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "mcp" / "computer_control.py"
args = sys.argv[1:]
py = args[args.index("--python") + 1] if "--python" in args else sys.executable
save = Path(args[args.index("--save") + 1]) if "--save" in args else None

p = subprocess.Popen([py, str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
_id = 0


def rpc(method, params=None, notify=False):
    global _id
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notify:
        _id += 1
        msg["id"] = _id
    p.stdin.write((json.dumps(msg) + "\n").encode())
    p.stdin.flush()
    return None if notify else json.loads(p.stdout.readline())


def call(name, **a):
    t = time.perf_counter()
    r = rpc("tools/call", {"name": name, "arguments": a})["result"]
    ms = (time.perf_counter() - t) * 1000
    kinds = [c["type"] for c in r["content"]]
    txt = " ".join(c["text"] for c in r["content"] if c["type"] == "text")
    print(f"- {name:18} {ms:6.0f} ms error={r['isError']!s:5} {kinds} :: {txt[:110]}")
    return r


init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
print("init:", init["result"]["serverInfo"], init["result"]["protocolVersion"])
rpc("notifications/initialized", notify=True)
tools = rpc("tools/list")["result"]["tools"]
print("tools:", len(tools))
assert rpc("ping")["result"] == {}
assert "error" in rpc("nope/method")

call("check_permissions")
call("list_displays")
r = call("screenshot")
img = next(c for c in r["content"] if c["type"] == "image")
if save:
    save.write_bytes(base64.b64decode(img["data"]))
call("screenshot", format="png", max_size=800)
pos = json.loads(call("cursor_position")["content"][0]["text"])
call("move_mouse", x=pos["x"], y=pos["y"])
call("zoom", x0=0, y0=0, x1=300, y1=60)
call("list_applications")
call("list_windows", app="Claude")
call("clipboard_get")
call("click", x=99999, y=5)              # expect out-of-range error
call("press_keys", keys="cmd+nonsense")  # expect unknown-key error
call("batch", actions=[{"action": "wait", "seconds": 0.1}], screenshot_after=False)
call("wait", seconds=0.1)
p.stdin.close()
p.wait(timeout=5)
print("exit", p.returncode)
