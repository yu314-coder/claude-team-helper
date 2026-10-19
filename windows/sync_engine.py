"""Everything Claude desktop keeps per account/org, and how to merge it between accounts.

The Python twin of SyncEngine.swift, used by the Windows app (and testable on any OS: every
function takes Claude's data folder). Each step only adds what is missing; nothing in the
destination is overwritten or removed.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

CATEGORIES = [
    ("codeSessions", "Code sessions (sidebar)"),
    ("archived", "Archived list"),
    ("routines", "Code routines (scheduled tasks)"),
    ("sections", "Sidebar groups"),
    ("coworkSessions", "Cowork sessions and their files"),
    ("coworkRoutines", "Cowork scheduled tasks"),
    ("coworkSettings", "Cowork settings"),
]
ALL = {c for c, _ in CATEGORIES}


def read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, obj: dict, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2 if pretty else None, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Store:
    """Claude's data folder (%APPDATA%\\Claude on Windows)."""

    def __init__(self, claude_dir: Path):
        self.root = Path(claude_dir)
        self.sessions = self.root / "claude-code-sessions"
        self.cowork = self.root / "local-agent-mode-sessions"
        self.config = self.root / "claude_desktop_config.json"

    def code_dir(self, key: str) -> Path:
        return self.sessions.joinpath(*key.split("/"))

    def cowork_dir(self, key: str) -> Path:
        return self.cowork.joinpath(*key.split("/"))


def session_files(d: Path) -> list[str]:
    return sorted(f.name for f in d.glob("local_*.json")) if d.is_dir() else []


def title_of(path: Path) -> str:
    d = read_json(path)
    return d.get("title") or Path(d.get("cwd") or path.name).name


def _archived(d: Path) -> list[str]:
    return read_json(d / "archived-sessions.idx").get("archived") or []


def _tasks(d: Path) -> list[dict]:
    return read_json(d / "scheduled-tasks.json").get("scheduledTasks") or []


def _missing_tasks(s: Path, d: Path) -> list[dict]:
    have = {t.get("id") for t in _tasks(d)}
    return [t for t in _tasks(s) if t.get("id") and t["id"] not in have]


def _sections(key: str, cfg: dict) -> list[dict]:
    ep = (cfg.get("preferences") or {}).get("epitaxyPrefs") or {}
    return ((ep.get("dframe-code-sections") or {}).get(key) or {}).get("sections") or []


def _missing_sections(s: str, d: str, cfg: dict) -> list[dict]:
    have = {x.get("id") for x in _sections(d, cfg)}
    return [x for x in _sections(s, cfg) if x.get("kind") == "manual" and x.get("id") not in have]


def _empty_settings(path: Path) -> bool:
    return not read_json(path)


def scan(store: Store) -> list[dict]:
    """Account/org folders, newest activity first."""
    out = []
    if not store.sessions.is_dir():
        return out
    for acct in store.sessions.iterdir():
        if not acct.is_dir():
            continue
        for org in acct.iterdir():
            if not org.is_dir():
                continue
            files = list(org.glob("local_*.json"))
            out.append({"id": f"{acct.name}/{org.name}", "account": acct.name, "org": org.name,
                        "count": len(files), "latest": max((f.stat().st_mtime for f in files), default=0)})
    out.sort(key=lambda f: f["latest"], reverse=True)
    return out


def plan(store: Store, s: str, d: str) -> dict:
    counts = {c: 0 for c in ALL}
    titles: list[str] = []
    if s == d:
        return {"counts": counts, "titles": titles}
    sc, dc = store.code_dir(s), store.code_dir(d)
    have = set(session_files(dc))
    new = [n for n in session_files(sc) if n not in have]
    counts["codeSessions"] = len(new)
    titles += [title_of(sc / n) for n in new]
    counts["archived"] = len(set(_archived(sc)) - set(_archived(dc)))
    counts["routines"] = len(_missing_tasks(sc, dc))
    counts["sections"] = len(_missing_sections(s, d, read_json(store.config)))
    sw, dw = store.cowork_dir(s), store.cowork_dir(d)
    have = set(session_files(dw))
    new = [n for n in session_files(sw) if n not in have]
    counts["coworkSessions"] = len(new)
    titles += ["Cowork: " + title_of(sw / n) for n in new]
    counts["coworkRoutines"] = len(_missing_tasks(sw, dw))
    counts["coworkSettings"] = int(not _empty_settings(sw / "cowork_settings.json")
                                   and _empty_settings(dw / "cowork_settings.json"))
    return {"counts": counts, "titles": titles}


def _copy_code_session(src: Path, dst: Path) -> None:
    """Copies a Code session record, dropping Remote Control bridge ids that belong to the old org."""
    data = read_json(src)
    if data.get("bridgeSessionIds"):
        data["bridgeSessionIds"] = []
        write_json(dst, data)
    else:
        shutil.copy2(src, dst)


def _merge_tasks(s: Path, d: Path) -> int:
    url = d / "scheduled-tasks.json"
    obj = read_json(url)
    add = _missing_tasks(s, d)
    obj["scheduledTasks"] = (obj.get("scheduledTasks") or []) + add
    write_json(url, obj, pretty=True)
    return len(add)


def apply(store: Store, cats: set[str], s: str, d: str, say: Callable[[str], None]) -> None:
    """Copies everything selected from s into d. Claude must be closed (it rewrites these files)."""
    p = plan(store, s, d)["counts"]

    def want(c: str) -> bool:
        return c in cats and p.get(c, 0) > 0

    sc, dc, sw, dw = store.code_dir(s), store.code_dir(d), store.cowork_dir(s), store.cowork_dir(d)
    if want("codeSessions"):
        dc.mkdir(parents=True, exist_ok=True)
        n = 0
        for name in session_files(sc):
            if not (dc / name).exists():
                _copy_code_session(sc / name, dc / name)
                n += 1
        say(f"Code sessions: copied {n}.")
    if want("archived"):
        url = dc / "archived-sessions.idx"
        obj = read_json(url) or {"v": 1}
        ids = obj.get("archived") or []
        add = [i for i in _archived(sc) if i not in ids]
        obj["archived"] = ids + add
        write_json(url, obj)
        say(f"Archived list: merged {len(add)}.")
    if want("routines"):
        say(f"Code routines: added {_merge_tasks(sc, dc)}.")
    if want("sections"):
        cfg = read_json(store.config)
        add = _missing_sections(s, d, cfg)
        ep = cfg.setdefault("preferences", {}).setdefault("epitaxyPrefs", {})
        entry = ep.setdefault("dframe-code-sections", {}).setdefault(d, {})
        lst = entry.setdefault("sections", [])
        nxt = max((x.get("order", 0) for x in lst), default=2) + 1
        for i, sec in enumerate(add):
            lst.append({**sec, "order": nxt + i})
        write_json(store.config, cfg, pretty=True)
        say(f"Sidebar groups: added {len(add)}.")
    if want("coworkSessions"):
        dw.mkdir(parents=True, exist_ok=True)
        n = 0
        # Session records name their own folder by absolute path; point them at the copy.
        # Paths appear raw and JSON-escaped (Windows backslashes are doubled in JSON).
        pairs = [(str(sw), str(dw)), (json.dumps(str(sw))[1:-1], json.dumps(str(dw))[1:-1])]
        for name in session_files(sw):
            if (dw / name).exists():
                continue
            sid = name[:-5]
            if (sw / sid).is_dir() and not (dw / sid).exists():
                shutil.copytree(sw / sid, dw / sid, symlinks=True)
            text = (sw / name).read_text(encoding="utf-8")
            for a, b in pairs:
                text = text.replace(a, b)
            (dw / name).write_text(text, encoding="utf-8")
            n += 1
        say(f"Cowork sessions: copied {n}.")
    if want("coworkRoutines"):
        say(f"Cowork scheduled tasks: added {_merge_tasks(sw, dw)}.")
    if want("coworkSettings"):
        dw.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sw / "cowork_settings.json", dw / "cowork_settings.json")
        say("Cowork settings: copied.")


def backup(store: Store, backups: Path, d: str, keep: int = 5) -> Path:
    """Backs up everything a sync into d can touch, and keeps the newest `keep` backups."""
    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S.%f")
    org = d.split("/")[-1][:8]
    target = backups / f"{stamp}_{org}"
    n = 2
    while target.exists():
        target = backups / f"{stamp}_{org}-{n}"
        n += 1
    target.mkdir(parents=True)
    if store.code_dir(d).is_dir():
        shutil.copytree(store.code_dir(d), target / "code", symlinks=True)
    if store.cowork_dir(d).is_dir():
        shutil.copytree(store.cowork_dir(d), target / "cowork", symlinks=True,
                        ignore=shutil.ignore_patterns("debug"))  # logs, not data
    if store.config.exists():
        shutil.copy2(store.config, target / "claude_desktop_config.json")
    for old in sorted((p for p in backups.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)[keep:]:
        shutil.rmtree(old, ignore_errors=True)
    return target


# ---------------------------------------------------------------------------
# Account names
# ---------------------------------------------------------------------------

def learn_names(store: Store, claude_json: Path, names_file: Path, folders: list[dict]) -> dict:
    """Org names and emails from what Claude stores locally, remembered across switches."""
    names = read_json(names_file)
    before = json.dumps(names, sort_keys=True)
    oa = read_json(claude_json).get("oauthAccount") or {}
    if oa.get("organizationUuid"):
        i = names.setdefault(oa["organizationUuid"], {})
        for k, src in (("orgName", "organizationName"), ("email", "emailAddress"), ("displayName", "displayName")):
            if oa.get(src):
                i[k] = oa[src]
    for f in folders:
        if (names.get(f["org"]) or {}).get("email"):
            continue
        for name in session_files(store.cowork_dir(f["id"])):
            o = read_json(store.cowork_dir(f["id"]) / name)
            if o.get("emailAddress"):
                i = names.setdefault(f["org"], {})
                i["email"] = o["emailAddress"]
                i.setdefault("displayName", o.get("accountName"))
                break
    if json.dumps(names, sort_keys=True) != before:
        write_json(names_file, names, pretty=True)
    return names


def set_nickname(names_file: Path, org: str, name: str) -> None:
    names = read_json(names_file)
    i = names.setdefault(org, {})
    if name.strip():
        i["nickname"] = name.strip()
    else:
        i.pop("nickname", None)
    write_json(names_file, names, pretty=True)


def label(folder: dict, names: dict, folders: list[dict]) -> tuple[str, str]:
    """A short title ("Personal", the org's name, your nickname) and a detail line."""
    info = dict(names.get(folder["org"]) or {})
    if not info.get("email"):  # orgs under one login share its email
        for f in folders:
            e = (names.get(f["org"]) or {}).get("email")
            if f["account"] == folder["account"] and e:
                info["email"] = e
                break
    personal = (info.get("orgName") or "").endswith("'s Organization")
    title = info.get("nickname") or ("Personal" if personal else info.get("orgName")) or "Unnamed organization"
    parts = []
    if info.get("displayName") and (personal or not info.get("orgName")):
        parts.append(info["displayName"])
    if info.get("email"):
        parts.append(info["email"])
    parts.append("org " + folder["org"][:8])
    if not info.get("nickname") and not info.get("orgName"):
        parts.append("name shows after you use it once, or click ✎")
    return title, " · ".join(parts)
