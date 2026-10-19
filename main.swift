import AppKit
import SwiftUI

// MARK: - Model

struct OrgFolder: Identifiable, Hashable {
    let url: URL
    let account: String
    let org: String
    let sessions: [String: URL]   // file name -> url
    let lastActivity: Date?
    var title = ""      // "Personal", the org's name, or your nickname
    var detail = ""     // name · email · org id

    var id: String { "\(account)/\(org)" }
    var shortName: String { "\(title) (\(org.prefix(8)))" }
}


enum Scanner {
    static func scan() -> [OrgFolder] {
        let fm = FileManager.default
        guard let accounts = try? fm.contentsOfDirectory(at: Paths.sessionsBase, includingPropertiesForKeys: nil) else { return [] }
        var result: [OrgFolder] = []
        for acct in accounts where acct.hasDirectoryPath {
            guard let orgs = try? fm.contentsOfDirectory(at: acct, includingPropertiesForKeys: nil) else { continue }
            for org in orgs where org.hasDirectoryPath {
                let files = (try? fm.contentsOfDirectory(at: org, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
                var sessions: [String: URL] = [:]
                var latest: Date?
                for f in files where f.lastPathComponent.hasPrefix("local_") && f.pathExtension == "json" {
                    sessions[f.lastPathComponent] = f
                    if let d = try? f.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate,
                       latest == nil || d > latest! { latest = d }
                }
                result.append(OrgFolder(url: org, account: acct.lastPathComponent, org: org.lastPathComponent,
                                        sessions: sessions, lastActivity: latest))
            }
        }
        let names = AccountNames.learn(orgs: result.map { OrgRef(account: $0.account, org: $0.org) })
        // Orgs under one login share its email, so an org whose name is not known yet still shows who it is.
        var emailByAccount: [String: String] = [:]
        for f in result { if let e = names[f.org]?.email { emailByAccount[f.account] = e } }
        for i in result.indices {
            var info = names[result[i].org] ?? AccountInfo()
            info.email = info.email ?? emailByAccount[result[i].account]
            (result[i].title, result[i].detail) = AccountNames.label(org: result[i].org, info: info)
        }
        return result.sorted { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }
    }

    static func title(of url: URL) -> String {
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return url.lastPathComponent }
        if let t = obj["title"] as? String, !t.isEmpty { return t }
        if let cwd = obj["cwd"] as? String { return (cwd as NSString).lastPathComponent }
        return url.lastPathComponent
    }
}

@MainActor
final class SyncModel: ObservableObject {
    @Published var folders: [OrgFolder] = []
    @Published var sourceID = ""
    @Published var destID = ""
    @Published var reopenClaude = true
    @Published var bothWays = false
    @Published var selected: Set<SyncCategory> = Set(SyncCategory.allCases)
    @Published var plan = SyncPlan()
    @Published var reversePlan = SyncPlan()
    @Published var busy = false
    @Published var log: [String] = []

    init() { refresh() }

    var source: OrgFolder? { folders.first { $0.id == sourceID } }
    var dest: OrgFolder? { folders.first { $0.id == destID } }
    private func ref(_ f: OrgFolder) -> OrgRef { OrgRef(account: f.account, org: f.org) }

    func count(_ c: SyncCategory) -> Int { plan.count(c) + (bothWays ? reversePlan.count(c) : 0) }
    var pending: Int { selected.reduce(0) { $0 + count($1) } }
    var titles: [String] { plan.titles + (bothWays ? reversePlan.titles : []) }

    func refresh() {
        folders = Scanner.scan()
        if dest == nil { destID = folders.first?.id ?? "" }
        if source == nil || sourceID == destID {
            // Default source: the other account with the most sessions missing from the destination.
            let d = dest
            sourceID = folders.filter { $0.id != destID }
                .max { a, b in
                    a.sessions.keys.filter { d?.sessions[$0] == nil }.count <
                    b.sessions.keys.filter { d?.sessions[$0] == nil }.count
                }?.id ?? ""
        }
        replan()
    }

    func replan() {
        guard let s = source, let d = dest, s.id != d.id else { plan = SyncPlan(); reversePlan = SyncPlan(); return }
        plan = SyncEngine.plan(from: ref(s), to: ref(d))
        reversePlan = SyncEngine.plan(from: ref(d), to: ref(s))
    }

    func say(_ s: String) {
        let t = DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium)
        log.append("[\(t)] \(s)")
    }

    func sync() async {
        guard let s = source, let d = dest, pending > 0 else { say("Nothing to sync."); return }
        busy = true
        defer { busy = false; refresh() }

        let wasRunning = ClaudeApp.isRunning
        if wasRunning {
            say("Quitting Claude… (confirm in Claude if it asks)")
            guard await ClaudeApp.quit() else {
                say("Claude did not quit. Nothing was changed. Quit it manually and press Sync again.")
                return
            }
        }
        let pairs = [(ref(s), ref(d))] + (bothWays ? [(ref(d), ref(s))] : [])
        for (from, into) in pairs {
            do {
                let b = try SyncEngine.backup(into)
                say("Backed up \(into.org.prefix(8)) → \(b.lastPathComponent)")
                try SyncEngine.apply(selected, from: from, to: into) { say("  " + $0) }
            } catch {
                say("Error: \(error.localizedDescription)")
            }
        }
        say("Done.")
        if wasRunning && reopenClaude {
            await ClaudeApp.open()
            say("Reopened Claude.")
        }
    }
}

// MARK: - Views

struct FolderRow: View {
    let folder: OrgFolder
    let isDest: Bool
    let isSource: Bool
    let isCurrent: Bool
    let rename: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: isDest ? "tray.and.arrow.down.fill" : isSource ? "tray.and.arrow.up.fill" : "tray")
                .foregroundStyle(isDest ? Color.accentColor : .secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(folder.title).font(.body.weight(.semibold))
                    Button(action: rename) { Image(systemName: "pencil") }
                        .buttonStyle(.borderless).help("Name this account")
                    if isCurrent {
                        Text("In use now").font(.caption2.bold())
                            .padding(.horizontal, 6).padding(.vertical, 1)
                            .background(Capsule().fill(Color.accentColor.opacity(0.18)))
                    }
                }
                Text(folder.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                Text(folder.lastActivity.map { "Last active \($0.formatted(.relative(presentation: .named)))" } ?? "No activity")
                    .font(.caption).foregroundStyle(.tertiary)
            }
            Spacer()
            Text("\(folder.sessions.count) sessions").font(.callout).monospacedDigit()
        }
        .padding(.vertical, 2)
    }
}

struct SyncView: View {
    @StateObject private var model = SyncModel()
    @State private var renaming: OrgFolder?
    @State private var newName = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Copy everything Claude keeps per account on this Mac into the account you're using.")
                    .font(.callout).foregroundStyle(.secondary)
                Spacer()
                Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                    .help("Rescan").disabled(model.busy)
            }

            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(model.folders) { f in
                        FolderRow(folder: f, isDest: f.id == model.destID, isSource: f.id == model.sourceID,
                                  isCurrent: f.id == model.folders.first?.id) {
                            newName = f.title == "Unnamed organization" ? "" : f.title
                            renaming = f
                        }
                        if f.id != model.folders.last?.id { Divider() }
                    }
                    if model.folders.isEmpty {
                        Text("No Claude session folders found.").foregroundStyle(.secondary)
                    }
                }.padding(6)
            } label: { Label("Accounts on this Mac", systemImage: "person.2") }

            HStack(spacing: 8) {
                Text("From").foregroundStyle(.secondary)
                Picker("", selection: $model.sourceID) {
                    ForEach(model.folders) { Text($0.shortName).tag($0.id) }
                }.labelsHidden()
                Image(systemName: model.bothWays ? "arrow.left.arrow.right" : "arrow.right").foregroundStyle(.secondary)
                Picker("", selection: $model.destID) {
                    ForEach(model.folders) { Text($0.shortName).tag($0.id) }
                }.labelsHidden()
            }
            .disabled(model.busy)
            .onChange(of: model.sourceID) { _, _ in model.replan() }
            .onChange(of: model.destID) { _, _ in model.replan() }

            GroupBox {
                VStack(spacing: 5) {
                    ForEach(SyncCategory.allCases) { c in
                        let n = model.count(c)
                        HStack {
                            Toggle(isOn: Binding(get: { model.selected.contains(c) },
                                                 set: { on in if on { model.selected.insert(c) } else { model.selected.remove(c) } })) {
                                Label(c.title, systemImage: c.icon)
                            }.toggleStyle(.checkbox)
                            Spacer()
                            Text(n == 0 ? "up to date" : "\(n) new")
                                .font(.callout).monospacedDigit()
                                .foregroundStyle(n == 0 ? Color.secondary : Color.primary)
                        }
                    }
                }.padding(6)
            } label: { Label("What gets synced", systemImage: "checklist") }
            .disabled(model.busy)

            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(model.pending == 0 ? "Everything is already in sync." : "\(model.pending) item\(model.pending == 1 ? "" : "s") to sync")
                        .font(.headline)
                    Toggle("Both ways (each account gets everything)", isOn: $model.bothWays)
                        .toggleStyle(.checkbox).font(.callout)
                    Toggle("Quit and reopen Claude automatically", isOn: $model.reopenClaude)
                        .toggleStyle(.checkbox).font(.callout)
                }
                Spacer()
                Button {
                    Task { await model.sync() }
                } label: {
                    HStack {
                        if model.busy { ProgressView().controlSize(.small) }
                        Text(model.busy ? "Syncing…" : model.selected.count == SyncCategory.allCases.count ? "Sync Everything" : "Sync Selected")
                    }.frame(minWidth: 140)
                }
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(model.pending == 0 || model.busy)
            }

            if !model.titles.isEmpty {
                DisclosureGroup("Show new sessions (\(model.titles.count))") {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(Array(model.titles.enumerated()), id: \.offset) { Text("• " + $0.element).font(.callout) }
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }.frame(maxHeight: 110)
                }
            }

            if !model.log.isEmpty {
                ScrollView {
                    Text(model.log.joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(height: 90)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
            }

            HStack {
                Text("Already shared by every account: pins, Claude Code memory, CLAUDE.md, settings and /stats. Each sync backs up first (last 5 kept).")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("Show Backups") {
                    try? FileManager.default.createDirectory(at: Paths.backups, withIntermediateDirectories: true)
                    NSWorkspace.shared.open(Paths.backups)
                }.controlSize(.small)
            }
        }
        .alert("Name this account", isPresented: Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })) {
            TextField("e.g. Team – Acme", text: $newName)
            Button("Save") {
                if let f = renaming { AccountNames.setNickname(newName, for: f.org) }
                renaming = nil
                model.refresh()
            }
            Button("Cancel", role: .cancel) { renaming = nil }
        } message: {
            Text(renaming.map { "Org \($0.org.prefix(8)). Leave empty to use the detected name." } ?? "")
        }
    }
}

struct RootView: View {
    @AppStorage("tab") private var tab = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(spacing: 14) {
                Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 52, height: 52)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Claude Team Helper").font(.title2.bold())
                    Text("Move to a Claude Team account without losing your setup.").foregroundStyle(.secondary)
                }
            }
            Picker("", selection: $tab) {
                Text("Account Sync").tag(0)
                Text("Computer Control").tag(1)
            }
            .pickerStyle(.segmented).labelsHidden()
            if tab == 0 { SyncView() } else { ComputerControlView() }
            Spacer(minLength: 0)
        }
        .padding(20)
        .frame(width: 620)
        .frame(minHeight: 780)
    }
}

@main
struct TeamHelperApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene {
        WindowGroup("Claude Team Helper") { RootView() }
            .windowResizability(.contentSize)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
