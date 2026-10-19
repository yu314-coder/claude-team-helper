// Sync Everything against a sandbox copy of Claude's data. Build and run with tests/run_sync_test.sh.
import Foundation

func check(_ ok: Bool, _ msg: String) { print((ok ? "  ✓ " : "  ✗ ") + msg); if !ok { exit(1) } }

let fm = FileManager.default
let orgs: [OrgRef] = ((try? fm.contentsOfDirectory(atPath: Paths.sessionsBase.path)) ?? []).flatMap { a in
    ((try? fm.contentsOfDirectory(atPath: Paths.sessionsBase.appendingPathComponent(a).path)) ?? [])
        .filter { !$0.hasPrefix(".") }.map { OrgRef(account: a, org: $0) }
}
check(orgs.count == 2, "found two accounts in the sandbox")
let (a, b) = (orgs[0], orgs[1])

// A custom sidebar group in A, so that path is exercised too.
var cfg = SyncEngine.readJSON(SyncEngine.desktopConfig)!
var prefs = cfg["preferences"] as! [String: Any]
var ep = prefs["epitaxyPrefs"] as! [String: Any]
var all = ep["dframe-code-sections"] as? [String: Any] ?? [:]
var entry = all[a.key] as? [String: Any] ?? [:]
var secs = entry["sections"] as? [[String: Any]] ?? []
secs.append(["id": "test-group", "kind": "manual", "name": "Test group", "order": 9, "v": 2])
entry["sections"] = secs; all[a.key] = entry; ep["dframe-code-sections"] = all
prefs["epitaxyPrefs"] = ep; cfg["preferences"] = prefs
try SyncEngine.writeJSON(cfg, to: SyncEngine.desktopConfig, pretty: true)

for (from, into) in [(a, b), (b, a)] {
    let p = SyncEngine.plan(from: from, to: into)
    print("\(from.org.prefix(8)) → \(into.org.prefix(8)):",
          SyncCategory.allCases.map { "\($0.rawValue)=\(p.count($0))" }.joined(separator: " "))
}
let before = SyncEngine.plan(from: a, to: b)
for (from, into) in [(a, b), (b, a)] {
    _ = try SyncEngine.backup(into)
    try SyncEngine.apply(Set(SyncCategory.allCases), from: from, to: into) { print("    " + $0) }
}
for (from, into) in [(a, b), (b, a)] {
    check(SyncEngine.plan(from: from, to: into).total == 0, "\(from.org.prefix(8)) → \(into.org.prefix(8)) fully in sync afterwards")
}

// Every record written is valid JSON, and copied Cowork records point at their own copy.
for ref in [a, b] {
    for dir in [ref.codeDir, ref.coworkDir] {
        for f in (try? fm.contentsOfDirectory(atPath: dir.path)) ?? [] where f.hasPrefix("local_") && f.hasSuffix(".json") {
            let text = try String(contentsOf: dir.appendingPathComponent(f), encoding: .utf8)
            if SyncEngine.readJSON(dir.appendingPathComponent(f)) == nil { check(false, "\(f) is valid JSON") }
            let other = (ref == a ? b : a).coworkDir.path
            if dir == ref.coworkDir && text.contains(other) { check(false, "\(f) no longer points at the other account") }
        }
    }
}
check(true, "all session records are valid JSON; Cowork paths rewritten")
check(SyncEngine.plan(from: b, to: a).count(.coworkSessions) == 0 && before.count(.coworkSessions) > 0 || before.count(.coworkSessions) == 0,
      "Cowork sessions and folders copied")
let ids = ((SyncEngine.readJSON(b.codeDir.appendingPathComponent("archived-sessions.idx"))?["archived"] as? [String]) ?? [])
check(Set(ids).count == ids.count, "archived list has no duplicates (\(ids.count) ids)")

// Backups prune to the newest five.
for _ in 0..<6 { _ = try SyncEngine.backup(b) }  // back to back, same second
let kept = try fm.contentsOfDirectory(atPath: Paths.backups.path).filter { !$0.hasPrefix(".") }
check(kept.count == 5, "backups pruned to 5 (have \(kept.count))")
print("ALL PASSED")
