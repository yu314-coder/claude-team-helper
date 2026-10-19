import Foundation

enum Paths {
    static let home = FileManager.default.homeDirectoryForCurrentUser
    // CTH_CLAUDE_SUPPORT points everything at a sandbox copy (used by tests/SyncEngineTest.swift).
    static let claudeSupport = ProcessInfo.processInfo.environment["CTH_CLAUDE_SUPPORT"].map { URL(fileURLWithPath: $0) }
        ?? home.appendingPathComponent("Library/Application Support/Claude")
    static let sessionsBase = claudeSupport.appendingPathComponent("claude-code-sessions")
    static let helperSupport = ProcessInfo.processInfo.environment["CTH_HELPER_SUPPORT"].map { URL(fileURLWithPath: $0) }
        ?? home.appendingPathComponent("Library/Application Support/ClaudeTeamHelper")
    static let backups = helperSupport.appendingPathComponent("backups")
    static let claudeBundleID = "com.anthropic.claudefordesktop"
}


/// Everything Claude desktop keeps per account/org on this Mac, and how to merge it from one
/// account into another. Every step only adds what is missing; nothing in the destination is
/// overwritten or removed. Copies use FileManager, which clones on APFS, so they cost no space.
enum SyncCategory: String, CaseIterable, Identifiable {
    case codeSessions, archived, routines, sections, coworkSessions, coworkRoutines, coworkSettings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .codeSessions: return "Code sessions (sidebar)"
        case .archived: return "Archived list"
        case .routines: return "Code routines (scheduled tasks)"
        case .sections: return "Sidebar groups"
        case .coworkSessions: return "Cowork sessions and their files"
        case .coworkRoutines: return "Cowork scheduled tasks"
        case .coworkSettings: return "Cowork settings"
        }
    }

    var icon: String {
        switch self {
        case .codeSessions: return "sidebar.left"
        case .archived: return "archivebox"
        case .routines: return "clock.arrow.circlepath"
        case .sections: return "folder"
        case .coworkSessions: return "person.2.wave.2"
        case .coworkRoutines: return "calendar.badge.clock"
        case .coworkSettings: return "gearshape"
        }
    }
}

struct OrgRef: Hashable {
    let account: String
    let org: String

    var key: String { "\(account)/\(org)" }
    var codeDir: URL { Paths.sessionsBase.appendingPathComponent(key) }
    var coworkDir: URL { Paths.claudeSupport.appendingPathComponent("local-agent-mode-sessions/\(key)") }
}

struct SyncPlan {
    var counts: [SyncCategory: Int] = [:]
    var titles: [String] = []   // new sessions, for the preview list

    var total: Int { counts.values.reduce(0, +) }
    func count(_ c: SyncCategory) -> Int { counts[c] ?? 0 }
}

enum SyncEngine {
    static let desktopConfig = Paths.claudeSupport.appendingPathComponent("claude_desktop_config.json")
    private static let fm = FileManager.default

    // MARK: JSON helpers

    static func readJSON(_ url: URL) -> [String: Any]? {
        guard let d = try? Data(contentsOf: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: d) as? [String: Any]
    }

    static func writeJSON(_ obj: [String: Any], to url: URL, pretty: Bool = false) throws {
        var opts: JSONSerialization.WritingOptions = [.withoutEscapingSlashes]
        if pretty { opts.insert(.prettyPrinted) }
        try JSONSerialization.data(withJSONObject: obj, options: opts).write(to: url, options: .atomic)
    }

    private static func sessionFiles(_ dir: URL) -> [String] {
        ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? [])
            .filter { $0.hasPrefix("local_") && $0.hasSuffix(".json") }.sorted()
    }

    private static func title(_ url: URL) -> String {
        let o = readJSON(url)
        if let t = o?["title"] as? String, !t.isEmpty { return t }
        if let cwd = o?["cwd"] as? String { return (cwd as NSString).lastPathComponent }
        return url.lastPathComponent
    }

    private static func archivedIDs(_ dir: URL) -> [String] {
        readJSON(dir.appendingPathComponent("archived-sessions.idx"))?["archived"] as? [String] ?? []
    }

    private static func tasks(_ dir: URL) -> [[String: Any]] {
        readJSON(dir.appendingPathComponent("scheduled-tasks.json"))?["scheduledTasks"] as? [[String: Any]] ?? []
    }

    private static func missingTasks(_ s: URL, _ d: URL) -> [[String: Any]] {
        let have = Set(tasks(d).compactMap { $0["id"] as? String })
        return tasks(s).filter { ($0["id"] as? String).map { !have.contains($0) } ?? false }
    }

    private static func sections(_ ref: OrgRef, in cfg: [String: Any]?) -> [[String: Any]] {
        let prefs = (cfg?["preferences"] as? [String: Any])?["epitaxyPrefs"] as? [String: Any]
        let all = prefs?["dframe-code-sections"] as? [String: Any]
        return (all?[ref.key] as? [String: Any])?["sections"] as? [[String: Any]] ?? []
    }

    /// Custom sidebar groups (built-in sections are Pinned, Routines and Sessions).
    private static func missingSections(_ s: OrgRef, _ d: OrgRef, _ cfg: [String: Any]?) -> [[String: Any]] {
        let have = Set(sections(d, in: cfg).compactMap { $0["id"] as? String })
        return sections(s, in: cfg).filter {
            ($0["kind"] as? String) == "manual" && !have.contains($0["id"] as? String ?? "")
        }
    }

    private static func isEmptySettings(_ url: URL) -> Bool {
        guard let o = readJSON(url) else { return true }
        return o.isEmpty
    }

    // MARK: Plan

    static func plan(from s: OrgRef, to d: OrgRef) -> SyncPlan {
        var p = SyncPlan()
        guard s != d else { return p }
        let haveCode = Set(sessionFiles(d.codeDir))
        let newCode = sessionFiles(s.codeDir).filter { !haveCode.contains($0) }
        p.counts[.codeSessions] = newCode.count
        p.titles += newCode.map { title(s.codeDir.appendingPathComponent($0)) }

        p.counts[.archived] = Set(archivedIDs(s.codeDir)).subtracting(archivedIDs(d.codeDir)).count
        p.counts[.routines] = missingTasks(s.codeDir, d.codeDir).count
        p.counts[.sections] = missingSections(s, d, readJSON(desktopConfig)).count

        let haveCowork = Set(sessionFiles(d.coworkDir))
        let newCowork = sessionFiles(s.coworkDir).filter { !haveCowork.contains($0) }
        p.counts[.coworkSessions] = newCowork.count
        p.titles += newCowork.map { "Cowork: " + title(s.coworkDir.appendingPathComponent($0)) }

        p.counts[.coworkRoutines] = missingTasks(s.coworkDir, d.coworkDir).count
        let sSet = s.coworkDir.appendingPathComponent("cowork_settings.json")
        let dSet = d.coworkDir.appendingPathComponent("cowork_settings.json")
        p.counts[.coworkSettings] = (!isEmptySettings(sSet) && isEmptySettings(dSet)) ? 1 : 0
        return p
    }

    // MARK: Apply

    /// Copies everything selected from `s` into `d`. Claude must be quit (it rewrites these files).
    static func apply(_ cats: Set<SyncCategory>, from s: OrgRef, to d: OrgRef, log: (String) -> Void) throws {
        let p = plan(from: s, to: d)
        func want(_ c: SyncCategory) -> Bool { cats.contains(c) && p.count(c) > 0 }

        if want(.codeSessions) {
            try fm.createDirectory(at: d.codeDir, withIntermediateDirectories: true)
            var n = 0
            for name in sessionFiles(s.codeDir) where !fm.fileExists(atPath: d.codeDir.appendingPathComponent(name).path) {
                try copyCodeSession(s.codeDir.appendingPathComponent(name), to: d.codeDir.appendingPathComponent(name))
                n += 1
            }
            log("Code sessions: copied \(n).")
        }
        if want(.archived) {
            let url = d.codeDir.appendingPathComponent("archived-sessions.idx")
            var obj = readJSON(url) ?? ["v": 1]
            var ids = obj["archived"] as? [String] ?? []
            let add = archivedIDs(s.codeDir).filter { !ids.contains($0) }
            ids += add
            obj["archived"] = ids
            try writeJSON(obj, to: url)
            log("Archived list: merged \(add.count).")
        }
        if want(.routines) { try mergeTasks(s.codeDir, d.codeDir, label: "Code routines", log: log) }
        if want(.sections) {
            var cfg = readJSON(desktopConfig) ?? [:]
            var prefs = cfg["preferences"] as? [String: Any] ?? [:]
            var ep = prefs["epitaxyPrefs"] as? [String: Any] ?? [:]
            var all = ep["dframe-code-sections"] as? [String: Any] ?? [:]
            var entry = all[d.key] as? [String: Any] ?? [:]
            var list = entry["sections"] as? [[String: Any]] ?? []
            let add = missingSections(s, d, cfg)
            let next = (list.compactMap { $0["order"] as? Int }.max() ?? 2) + 1
            for (i, var sec) in add.enumerated() {
                sec["order"] = next + i
                list.append(sec)
            }
            entry["sections"] = list
            all[d.key] = entry
            ep["dframe-code-sections"] = all
            prefs["epitaxyPrefs"] = ep
            cfg["preferences"] = prefs
            try writeJSON(cfg, to: desktopConfig, pretty: true)
            log("Sidebar groups: added \(add.count).")
        }
        if want(.coworkSessions) {
            try fm.createDirectory(at: d.coworkDir, withIntermediateDirectories: true)
            var n = 0
            for name in sessionFiles(s.coworkDir) where !fm.fileExists(atPath: d.coworkDir.appendingPathComponent(name).path) {
                let id = (name as NSString).deletingPathExtension
                let folder = s.coworkDir.appendingPathComponent(id)
                let target = d.coworkDir.appendingPathComponent(id)
                if fm.fileExists(atPath: folder.path) && !fm.fileExists(atPath: target.path) {
                    try fm.copyItem(at: folder, to: target)  // APFS clone: no extra space
                }
                // Session records name their own folder by absolute path; point them at the copy.
                var text = try String(contentsOf: s.coworkDir.appendingPathComponent(name), encoding: .utf8)
                text = text.replacingOccurrences(of: s.coworkDir.path, with: d.coworkDir.path)
                    .replacingOccurrences(of: s.coworkDir.path.replacingOccurrences(of: "/", with: "\\/"),
                                          with: d.coworkDir.path.replacingOccurrences(of: "/", with: "\\/"))
                try text.write(to: d.coworkDir.appendingPathComponent(name), atomically: true, encoding: .utf8)
                n += 1
            }
            log("Cowork sessions: copied \(n).")
        }
        if want(.coworkRoutines) { try mergeTasks(s.coworkDir, d.coworkDir, label: "Cowork scheduled tasks", log: log) }
        if want(.coworkSettings) {
            try fm.createDirectory(at: d.coworkDir, withIntermediateDirectories: true)
            let dst = d.coworkDir.appendingPathComponent("cowork_settings.json")
            try? fm.removeItem(at: dst)
            try fm.copyItem(at: s.coworkDir.appendingPathComponent("cowork_settings.json"), to: dst)
            log("Cowork settings: copied.")
        }
    }

    /// Copies a Code session record, dropping Remote Control bridge ids that belong to the old org.
    private static func copyCodeSession(_ from: URL, to: URL) throws {
        let data = try Data(contentsOf: from)
        if var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let bridges = obj["bridgeSessionIds"] as? [Any], !bridges.isEmpty {
            obj["bridgeSessionIds"] = [Any]()
            try JSONSerialization.data(withJSONObject: obj).write(to: to, options: .atomic)
        } else {
            try data.write(to: to, options: .atomic)
        }
    }

    private static func mergeTasks(_ s: URL, _ d: URL, label: String, log: (String) -> Void) throws {
        try fm.createDirectory(at: d, withIntermediateDirectories: true)
        let url = d.appendingPathComponent("scheduled-tasks.json")
        var obj = readJSON(url) ?? [:]
        let add = missingTasks(s, d)
        obj["scheduledTasks"] = (obj["scheduledTasks"] as? [[String: Any]] ?? []) + add
        try writeJSON(obj, to: url, pretty: true)
        log("\(label): added \(add.count).")
    }

    // MARK: Backup

    /// Backs up everything a sync into `d` can touch, and keeps the newest `keep` backups.
    static func backup(_ d: OrgRef, keep: Int = 5) throws -> URL {
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]  // two syncs in one second stay apart
        let stamp = iso.string(from: Date()).replacingOccurrences(of: ":", with: "-")
        var target = Paths.backups.appendingPathComponent("\(stamp)_\(d.org.prefix(8))")
        var n = 2
        while fm.fileExists(atPath: target.path) {
            target = Paths.backups.appendingPathComponent("\(stamp)_\(d.org.prefix(8))-\(n)")
            n += 1
        }
        try fm.createDirectory(at: target, withIntermediateDirectories: true)
        if fm.fileExists(atPath: d.codeDir.path) {
            try fm.copyItem(at: d.codeDir, to: target.appendingPathComponent("code"))
        }
        if fm.fileExists(atPath: d.coworkDir.path) {
            try fm.copyItem(at: d.coworkDir, to: target.appendingPathComponent("cowork"))
        }
        if fm.fileExists(atPath: desktopConfig.path) {
            try fm.copyItem(at: desktopConfig, to: target.appendingPathComponent("claude_desktop_config.json"))
        }
        let all = ((try? fm.contentsOfDirectory(at: Paths.backups, includingPropertiesForKeys: nil)) ?? [])
            .filter(\.hasDirectoryPath).sorted { $0.lastPathComponent > $1.lastPathComponent }
        for old in all.dropFirst(keep) { try? fm.removeItem(at: old) }
        return target
    }
}

// MARK: - Account names

/// Human names for the account/org folders, which on disk are only UUIDs. Learned from what
/// Claude stores locally (the signed-in org in ~/.claude.json, the account on Cowork sessions)
/// and remembered, so an org keeps its name after you switch away from it. Nicknames win.
struct AccountInfo: Codable {
    var orgName: String?
    var email: String?
    var displayName: String?
    var nickname: String?

    var isPersonal: Bool { orgName?.hasSuffix("'s Organization") ?? false }
}

enum AccountNames {
    static let file = Paths.helperSupport.appendingPathComponent("account-names.json")

    static func load() -> [String: AccountInfo] {
        guard let d = try? Data(contentsOf: file) else { return [:] }
        return (try? JSONDecoder().decode([String: AccountInfo].self, from: d)) ?? [:]
    }

    static func save(_ all: [String: AccountInfo]) {
        try? FileManager.default.createDirectory(at: Paths.helperSupport, withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        try? enc.encode(all).write(to: file, options: .atomic)
    }

    /// Reads what Claude has on disk right now and merges it into the remembered names.
    @discardableResult
    static func learn(orgs: [OrgRef]) -> [String: AccountInfo] {
        var all = load()
        let before = (try? JSONEncoder().encode(all)) ?? Data()
        if let oa = SyncEngine.readJSON(Paths.home.appendingPathComponent(".claude.json"))?["oauthAccount"] as? [String: Any],
           let org = oa["organizationUuid"] as? String {
            var i = all[org] ?? AccountInfo()
            i.orgName = oa["organizationName"] as? String ?? i.orgName
            i.email = oa["emailAddress"] as? String ?? i.email
            i.displayName = oa["displayName"] as? String ?? i.displayName
            all[org] = i
        }
        for ref in orgs where all[ref.org]?.email == nil {
            let files = ((try? FileManager.default.contentsOfDirectory(atPath: ref.coworkDir.path)) ?? [])
                .filter { $0.hasPrefix("local_") && $0.hasSuffix(".json") }
            for f in files {
                let o = SyncEngine.readJSON(ref.coworkDir.appendingPathComponent(f))
                if let email = o?["emailAddress"] as? String {
                    var i = all[ref.org] ?? AccountInfo()
                    i.email = email
                    i.displayName = i.displayName ?? o?["accountName"] as? String
                    all[ref.org] = i
                    break
                }
            }
        }
        if (try? JSONEncoder().encode(all)) != before { save(all) }
        return all
    }

    static func setNickname(_ name: String, for org: String) {
        var all = load()
        var i = all[org] ?? AccountInfo()
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        i.nickname = trimmed.isEmpty ? nil : trimmed
        all[org] = i
        save(all)
    }

    /// A short title ("Personal", "Acme Inc", your nickname) and a detail line (email · org id).
    static func label(org: String, info: AccountInfo?) -> (title: String, detail: String) {
        let short = String(org.prefix(8))
        let title = info?.nickname ?? (info?.isPersonal == true ? "Personal" : info?.orgName) ?? "Unnamed organization"
        var parts: [String] = []
        if let n = info?.displayName, info?.isPersonal == true || info?.orgName == nil { parts.append(n) }
        if let e = info?.email { parts.append(e) }
        parts.append("org \(short)")
        if info?.nickname == nil && info?.orgName == nil { parts.append("name shows after you use it once, or click ✎") }
        return (title, parts.joined(separator: " · "))
    }
}
