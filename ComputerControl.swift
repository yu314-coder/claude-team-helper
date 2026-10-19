import AppKit
import SwiftUI

// MARK: - Claude app control (shared with Sidebar Sync)

enum ClaudeApp {
    static var isRunning: Bool {
        !NSRunningApplication.runningApplications(withBundleIdentifier: Paths.claudeBundleID).isEmpty
    }

    /// Asks Claude to quit and waits up to 60 s (Claude may show its own confirm dialog).
    static func quit() async -> Bool {
        for app in NSRunningApplication.runningApplications(withBundleIdentifier: Paths.claudeBundleID) {
            app.terminate()
        }
        for _ in 0..<120 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if !isRunning { return true }
        }
        return false
    }

    static func open() async {
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: Paths.claudeBundleID) {
            _ = try? await NSWorkspace.shared.openApplication(at: url, configuration: .init())
        }
    }
}

// MARK: - Locations and tools

enum CC {
    static let serverName = "computer-control"
    static let dir = Paths.helperSupport.appendingPathComponent("computer-control")
    static let venv = dir.appendingPathComponent("venv")
    static let python = venv.appendingPathComponent("bin/python")
    static let script = dir.appendingPathComponent("computer_control.py")
    static let requirements = dir.appendingPathComponent("requirements-mac.txt")
    static let claudeJSON = Paths.home.appendingPathComponent(".claude.json")
    static let desktopConfig = Paths.claudeSupport.appendingPathComponent("claude_desktop_config.json")

    private static let home = Paths.home.path

    private static func firstExecutable(_ paths: [String]) -> String? {
        paths.first { FileManager.default.isExecutableFile(atPath: $0) }
    }

    static var uv: String? {
        firstExecutable(["/opt/homebrew/bin/uv", "\(home)/.local/bin/uv", "/usr/local/bin/uv", "\(home)/.cargo/bin/uv"])
    }

    static var claudeCLI: String? {
        firstExecutable(["\(home)/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude"])
    }

    /// A Python 3.10+ to build the venv with when uv is not installed.
    static var systemPython: String? {
        firstExecutable(["3.13", "3.12", "3.11", "3.10"].flatMap {
            ["/opt/homebrew/bin/python\($0)", "/usr/local/bin/python\($0)",
             "/Library/Frameworks/Python.framework/Versions/\($0)/bin/python3"]
        })
    }

    static var installedVersion: String? {
        guard FileManager.default.isExecutableFile(atPath: python.path),
              let src = try? String(contentsOf: script, encoding: .utf8),
              let line = src.split(separator: "\n").first(where: { $0.hasPrefix("VERSION = ") }) else { return nil }
        return line.replacingOccurrences(of: "VERSION = ", with: "").trimmingCharacters(in: CharacterSet(charactersIn: "\" "))
    }

    static func jsonObject(at url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    }

    /// Disk space the server and its environment use.
    static var diskUsage: Int64 {
        guard let e = FileManager.default.enumerator(at: dir, includingPropertiesForKeys: [.totalFileAllocatedSizeKey]) else { return 0 }
        var total: Int64 = 0
        for case let u as URL in e {
            total += Int64((try? u.resourceValues(forKeys: [.totalFileAllocatedSizeKey]).totalFileAllocatedSize) ?? 0)
        }
        return total
    }

    static var registeredForCode: Bool {
        ((jsonObject(at: claudeJSON)?["mcpServers"] as? [String: Any])?[serverName]) != nil
    }

    static var registeredForChat: Bool {
        ((jsonObject(at: desktopConfig)?["mcpServers"] as? [String: Any])?[serverName]) != nil
    }

    /// Adds or removes our entry in claude_desktop_config.json. Only call while Claude is quit.
    static func setChatRegistration(_ on: Bool) throws {
        var obj = jsonObject(at: desktopConfig) ?? [:]
        var servers = obj["mcpServers"] as? [String: Any] ?? [:]
        if on {
            servers[serverName] = ["command": python.path, "args": [script.path]]
        } else {
            servers.removeValue(forKey: serverName)
        }
        obj["mcpServers"] = servers
        let data = try JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .withoutEscapingSlashes])
        try data.write(to: desktopConfig, options: .atomic)
    }
}

/// Runs a command, streaming its output lines to `log`. Returns the exit status.
func runProcess(_ exe: String, _ args: [String], log: @escaping @MainActor (String) -> Void) async -> Int32 {
    await withCheckedContinuation { cont in
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:\(Paths.home.path)/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { h in
            let data = h.availableData
            guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            for line in s.split(separator: "\n") where !line.trimmingCharacters(in: .whitespaces).isEmpty {
                let l = String(line)
                Task { @MainActor in log(l) }
            }
        }
        p.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            cont.resume(returning: proc.terminationStatus)
        }
        do { try p.run() } catch {
            Task { @MainActor in log("Could not run \(exe): \(error.localizedDescription)") }
            pipe.fileHandleForReading.readabilityHandler = nil
            cont.resume(returning: -1)
        }
    }
}

// MARK: - Model

@MainActor
final class ComputerControlModel: ObservableObject {
    @Published var version: String?
    @Published var size: String = ""
    @Published var forCode = false
    @Published var forChat = false
    @Published var alsoChat = false
    @Published var busy = false
    @Published var log: [String] = []

    init() { refresh() }

    func refresh() {
        version = CC.installedVersion
        size = version == nil ? "" : ByteCountFormatter.string(fromByteCount: CC.diskUsage, countStyle: .file)
        forCode = CC.registeredForCode
        forChat = CC.registeredForChat
    }

    func say(_ s: String) { log.append(s) }

    private func step(_ title: String, _ exe: String, _ args: [String]) async -> Bool {
        say("▸ \(title)")
        let code = await runProcess(exe, args) { [weak self] in self?.say("  \($0)") }
        if code != 0 { say("✗ \(title) failed (exit \(code)).") }
        return code == 0
    }

    func install() async {
        busy = true
        log = []
        defer { busy = false; refresh() }
        let fm = FileManager.default
        do {
            try fm.createDirectory(at: CC.dir, withIntermediateDirectories: true)
            for name in ["computer_control.py", "requirements-mac.txt"] {
                guard let src = Bundle.main.url(forResource: (name as NSString).deletingPathExtension,
                                                withExtension: (name as NSString).pathExtension) else {
                    say("✗ \(name) is missing from the app bundle."); return
                }
                let dst = CC.dir.appendingPathComponent(name)
                try? fm.removeItem(at: dst)
                try fm.copyItem(at: src, to: dst)
            }
            say("▸ Copied server to \(CC.dir.path.replacingOccurrences(of: Paths.home.path, with: "~"))")
        } catch {
            say("✗ \(error.localizedDescription)"); return
        }

        // Always a fresh environment, so an update never leaves old packages behind.
        try? fm.removeItem(at: CC.venv)
        // Only what pyautogui needs on macOS (--no-deps skips the full ~100 MB pyobjc), and no
        // download cache, so nothing piles up outside this folder.
        if let uv = CC.uv {
            guard await step("Creating Python environment", uv,
                             ["venv", "--python", CC.systemPython ?? "3.12", CC.venv.path]) else { return }
            guard await step("Installing pyautogui", uv,
                             ["pip", "install", "--no-cache", "--no-deps", "--python", CC.python.path,
                              "-r", CC.requirements.path]) else { return }
        } else if let py = CC.systemPython {
            guard await step("Creating Python environment", py, ["-m", "venv", "--without-pip", CC.venv.path]) else { return }
            guard await step("Installing pyautogui", py,
                             ["-m", "pip", "--python", CC.python.path, "install", "--no-cache-dir", "--no-deps",
                              "--disable-pip-version-check", "-r", CC.requirements.path]) else { return }
        } else {
            say("✗ Need Python 3.10+ or uv. Install one with: brew install uv"); return
        }

        // pyobjc-core ships its own 16 MB test suite.
        if let libs = try? fm.contentsOfDirectory(at: CC.venv.appendingPathComponent("lib"), includingPropertiesForKeys: nil) {
            for lib in libs { try? fm.removeItem(at: lib.appendingPathComponent("site-packages/PyObjCTest")) }
        }

        guard await step("Checking the server", CC.python.path, [CC.script.path, "--check"]) else { return }

        if let claude = CC.claudeCLI {
            _ = await runProcess(claude, ["mcp", "remove", "--scope", "user", CC.serverName]) { _ in }
            guard await step("Registering with Claude Code", claude,
                             ["mcp", "add", "--scope", "user", CC.serverName, "--", CC.python.path, CC.script.path]) else { return }
        } else {
            say("! Claude Code CLI not found, so the Code tab was not registered. Install it, then press Install again.")
        }

        if alsoChat { await setChat(true) }
        say("✓ Done. Start a new Claude session to use the computer-control tools.")
    }

    func uninstall() async {
        busy = true
        log = []
        defer { busy = false; refresh() }
        if let claude = CC.claudeCLI, CC.registeredForCode {
            _ = await step("Removing from Claude Code", claude, ["mcp", "remove", "--scope", "user", CC.serverName])
        }
        if CC.registeredForChat { await setChat(false) }
        do {
            if FileManager.default.fileExists(atPath: CC.dir.path) { try FileManager.default.removeItem(at: CC.dir) }
            say("✓ Removed the server and its Python environment.")
        } catch { say("✗ \(error.localizedDescription)") }
    }

    /// Claude rewrites its desktop config while running, so edit it only while Claude is quit.
    private func setChat(_ on: Bool) async {
        let wasRunning = ClaudeApp.isRunning
        if wasRunning {
            say("▸ Quitting Claude to update its chat config… (confirm in Claude if it asks)")
            guard await ClaudeApp.quit() else { say("✗ Claude did not quit; chat config left unchanged."); return }
        }
        do {
            try CC.setChatRegistration(on)
            say(on ? "✓ Added to Claude chat config." : "✓ Removed from Claude chat config.")
        } catch { say("✗ Chat config: \(error.localizedDescription)") }
        if wasRunning { await ClaudeApp.open(); say("▸ Reopened Claude.") }
    }

    func test() async {
        busy = true
        log = []
        defer { busy = false }
        guard FileManager.default.isExecutableFile(atPath: CC.python.path) else { say("Not installed yet."); return }
        _ = await step("Checking the server", CC.python.path, [CC.script.path, "--check"])
    }
}

// MARK: - View

struct StatusRow: View {
    let ok: Bool
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: ok ? "checkmark.circle.fill" : "circle.dashed")
                .foregroundStyle(ok ? Color.green : .secondary)
            Text(title)
            Spacer()
            Text(detail).font(.callout).foregroundStyle(.secondary)
        }
    }
}

struct ComputerControlView: View {
    @StateObject private var model = ComputerControlModel()

    private func openSettings(_ pane: String) {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)")!)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Team plans don't include Anthropic's built-in computer use yet. If Claude → Settings → General shows a Computer use toggle, use that instead. Otherwise this installs a pyautogui MCP server so Claude can see the screen and use the mouse and keyboard.")
                .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)

            GroupBox {
                VStack(spacing: 8) {
                    StatusRow(ok: model.version != nil, title: "Server installed",
                              detail: model.version.map { "v\($0) · \(model.size)" } ?? "not installed")
                    Divider()
                    StatusRow(ok: model.forCode, title: "Available in Claude Code (Code tab, terminal)",
                              detail: model.forCode ? "registered" : "not registered")
                    Divider()
                    StatusRow(ok: model.forChat, title: "Available in Claude chat",
                              detail: model.forChat ? "registered" : "optional")
                }.padding(6)
            } label: { Label("Computer Control MCP", systemImage: "cursorarrow.rays") }

            HStack {
                Toggle("Also add to Claude chat (quits and reopens Claude)", isOn: $model.alsoChat)
                    .toggleStyle(.checkbox).font(.callout)
                Spacer()
                if model.version != nil {
                    Button("Test") { Task { await model.test() } }.disabled(model.busy)
                    Button("Uninstall", role: .destructive) { Task { await model.uninstall() } }.disabled(model.busy)
                }
                Button {
                    Task { await model.install() }
                } label: {
                    HStack {
                        if model.busy { ProgressView().controlSize(.small) }
                        Text(model.version == nil ? "Install" : "Reinstall / Update")
                    }.frame(minWidth: 120)
                }
                .controlSize(.large).buttonStyle(.borderedProminent).disabled(model.busy)
            }

            GroupBox {
                VStack(alignment: .leading, spacing: 8) {
                    Text("macOS must allow **Claude** (and Terminal, if you run `claude` there) to control the computer and record the screen.")
                        .font(.callout).fixedSize(horizontal: false, vertical: true)
                    HStack {
                        Button("Open Accessibility") { openSettings("Privacy_Accessibility") }
                        Button("Open Screen Recording") { openSettings("Privacy_ScreenCapture") }
                        Spacer()
                        Button("Copy test prompt") {
                            NSPasteboard.general.clearContents()
                            NSPasteboard.general.setString(
                                "Use the computer-control tools: run check_permissions, take a screenshot, and tell me what apps are open.",
                                forType: .string)
                        }
                    }.controlSize(.small)
                    Text("Emergency stop: move the mouse into any screen corner.")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(6)
            } label: { Label("Permissions & safety", systemImage: "lock.shield") }

            if !model.log.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 1) {
                            ForEach(Array(model.log.enumerated()), id: \.offset) { i, line in
                                Text(line).font(.system(.caption, design: .monospaced)).id(i)
                            }
                        }
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .onChange(of: model.log.count) { _, n in proxy.scrollTo(n - 1, anchor: .bottom) }
                }
                .frame(height: 110)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color(nsColor: .textBackgroundColor)))
            }
        }
    }
}
