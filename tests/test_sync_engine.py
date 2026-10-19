"""Sync Everything (Python engine, used on Windows) against a sandbox copy of Claude's data.
Run through tests/run_sync_test.sh."""
import json, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "windows"))
import sync_engine as se


def check(ok, msg):
    print(("  ✓ " if ok else "  ✗ ") + msg)
    if not ok:
        sys.exit(1)


store = se.Store(Path(os.environ["CTH_CLAUDE_SUPPORT"]))
backups = Path(os.environ["CTH_HELPER_SUPPORT"]) / "backups"
folders = se.scan(store)
check(len(folders) == 2, "found two accounts in the sandbox")
a, b = folders[0]["id"], folders[1]["id"]

cfg = se.read_json(store.config)
ep = cfg["preferences"]["epitaxyPrefs"]
ep.setdefault("dframe-code-sections", {}).setdefault(a, {}).setdefault("sections", []).append(
    {"id": "test-group", "kind": "manual", "name": "Test group", "order": 9, "v": 2})
se.write_json(store.config, cfg, pretty=True)

for s, d in ((a, b), (b, a)):
    c = se.plan(store, s, d)["counts"]
    print(f"{s[-36:-28]} → {d[-36:-28]}:", " ".join(f"{k}={c[k]}" for k, _ in se.CATEGORIES))
before = se.plan(store, b, a)["counts"]
for s, d in ((a, b), (b, a)):
    se.backup(store, backups, d)
    se.apply(store, se.ALL, s, d, lambda m: print("    " + m))
for s, d in ((a, b), (b, a)):
    check(sum(se.plan(store, s, d)["counts"].values()) == 0, f"{s[-36:-28]} → {d[-36:-28]} fully in sync afterwards")

for f in (a, b):
    other = str(store.cowork_dir(b if f == a else a))
    for d in (store.code_dir(f), store.cowork_dir(f)):
        for n in se.session_files(d):
            text = (d / n).read_text(encoding="utf-8")
            json.loads(text)
            if d == store.cowork_dir(f) and other in text:
                check(False, f"{n} no longer points at the other account")
check(True, "all session records are valid JSON; Cowork paths rewritten")
ids = se.read_json(store.code_dir(a) / "archived-sessions.idx").get("archived", [])
check(len(ids) == len(set(ids)), f"archived list has no duplicates ({len(ids)} ids)")
for _ in range(6):
    se.backup(store, backups, b)
check(len([p for p in backups.iterdir() if p.is_dir()]) == 5, "backups pruned to 5, back-to-back names unique")

names_file = Path(os.environ["CTH_HELPER_SUPPORT"]) / "account-names.json"
names = se.learn_names(store, Path.home() / ".claude.json", names_file, folders)
labels = [se.label(f, names, folders) for f in folders]
print("  labels:", labels)
check(any(t == "Personal" for t, _ in labels), "personal org is labelled Personal")
se.set_nickname(names_file, folders[1]["org"], "Team – Test")
names = se.read_json(names_file)
check(se.label(folders[1], names, folders)[0] == "Team – Test", "nickname wins")
print("ALL PASSED")
