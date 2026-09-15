import AppKit
import Darwin
import Foundation

var terminationWriteFD: Int32 = -1

func wakeForTermination(_: Int32) {
    var byte: UInt8 = 1
    _ = withUnsafePointer(to: &byte) { Darwin.write(terminationWriteFD, $0, 1) }
}

struct Representation: Equatable {
    let type: String
    let data: Data
}

struct ItemSnapshot: Equatable {
    let representations: [Representation]
}

struct Snapshot: Equatable {
    let changeCount: Int
    let items: [ItemSnapshot]
}

func stableSnapshot(_ board: NSPasteboard) -> Snapshot? {
    let before = board.changeCount
    let items = board.pasteboardItems ?? []
    var copied: [ItemSnapshot] = []
    for item in items {
        var representations: [Representation] = []
        for type in item.types {
            guard let data = item.data(forType: type) else { return nil }
            representations.append(Representation(type: type.rawValue, data: data))
        }
        copied.append(ItemSnapshot(representations: representations))
    }
    guard board.changeCount == before else { return nil }
    return Snapshot(changeCount: before, items: copied)
}

func pasteboardItems(_ snapshot: Snapshot) -> [NSPasteboardItem] {
    snapshot.items.map { saved in
        let item = NSPasteboardItem()
        for representation in saved.representations {
            item.setData(representation.data, forType: NSPasteboard.PasteboardType(representation.type))
        }
        return item
    }
}

func exactSyntheticItem(_ snapshot: Snapshot, expected: String) -> Bool {
    let permitted = Set([NSPasteboard.PasteboardType.string.rawValue, "public.text", "NSStringPboardType"])
    return snapshot.items.count == 1 && !snapshot.items[0].representations.isEmpty
        && snapshot.items[0].representations.allSatisfy {
            permitted.contains($0.type) && String(data: $0.data, encoding: .utf8) == expected
        }
}

func emit(_ receipt: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys]) else { return }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([10]))
}

final class ClipboardSession {
    private let board: NSPasteboard
    private let original: Snapshot?
    private let failAfterClear: Bool
    private var owned: Snapshot?
    private var terminal: [String: Any]?

    init(_ board: NSPasteboard, snapshotAvailable: Bool = true, failAfterClear: Bool = false) {
        self.board = board
        self.original = snapshotAvailable ? stableSnapshot(board) : nil
        self.failAfterClear = failAfterClear
    }

    private func unavailable() -> [String: Any] {
        ["status": "UNVERIFIED", "reason": "original-snapshot-unavailable"]
    }

    func readiness() -> [String: Any] {
        guard let original else {
            return ["status": "UNVERIFIED", "event": "ready", "reason": "original-snapshot-unavailable"]
        }
        return ["status": "PASS", "event": "ready", "change_count": original.changeCount]
    }

    func seed(_ nonce: String) -> [String: Any] {
        guard terminal == nil else { return ["status": "FAIL", "reason": "clipboard-session-finalized"] }
        guard !nonce.isEmpty, nonce.utf8.count <= 1024 else {
            return ["status": "FAIL", "reason": "invalid-synthetic-nonce"]
        }
        guard let original else { return unavailable() }
        guard owned == nil else { return ["status": "FAIL", "reason": "synthetic-state-already-active"] }
        guard let current = stableSnapshot(board) else {
            return ["status": "UNVERIFIED", "reason": "clipboard-snapshot-unstable"]
        }
        guard current == original else {
            return ["status": "UNVERIFIED", "reason": "clipboard-ownership-changed"]
        }
        let clearCount = board.clearContents()
        guard clearCount != 0, let cleared = stableSnapshot(board), cleared.changeCount == clearCount, cleared.items.isEmpty else {
            return ["status": "FAIL", "reason": "synthetic-clear-failed"]
        }
        owned = cleared
        if failAfterClear { return seedFailure("synthetic-write-failed") }
        let item = NSPasteboardItem()
        item.setString(nonce, forType: .string)
        guard board.writeObjects([item]), let claimed = stableSnapshot(board), exactSyntheticItem(claimed, expected: nonce) else {
            return seedFailure("synthetic-write-failed")
        }
        owned = claimed
        return ["status": "PASS", "change_count": claimed.changeCount, "synthetic": "seeded"]
    }

    func check(_ expected: String) -> [String: Any] {
        guard terminal == nil else { return ["status": "FAIL", "reason": "clipboard-session-finalized"] }
        guard !expected.isEmpty, expected.utf8.count <= 1024 else {
            return ["status": "FAIL", "reason": "invalid-expected-synthetic"]
        }
        guard original != nil else { return unavailable() }
        guard let current = stableSnapshot(board) else {
            return ["status": "UNVERIFIED", "reason": "clipboard-snapshot-unstable"]
        }
        guard exactSyntheticItem(current, expected: expected) else {
            return ["status": "UNVERIFIED", "change_count": current.changeCount, "synthetic": "mismatch"]
        }
        owned = current
        return ["status": "PASS", "change_count": current.changeCount, "synthetic": "matched"]
    }

    private func seedFailure(_ reason: String) -> [String: Any] {
        let result: [String: Any] = ["status": "FAIL", "reason": reason, "restoration": performRestore()]
        terminal = result
        return result
    }

    private func performRestore() -> [String: Any] {
        guard let original else { return unavailable() }
        guard let owned else { return ["status": "UNVERIFIED", "reason": "no-owned-clipboard-state"] }
        guard let current = stableSnapshot(board) else {
            return ["status": "UNVERIFIED", "reason": "clipboard-snapshot-unstable"]
        }
        guard current == owned else {
            return ["status": "UNVERIFIED", "reason": "clipboard-ownership-changed", "change_count": current.changeCount]
        }
        guard board.clearContents() != 0, board.writeObjects(pasteboardItems(original)), let restored = stableSnapshot(board), restored.items == original.items else {
            return ["status": "FAIL", "reason": "clipboard-restore-failed"]
        }
        return ["status": "PASS", "restoration": "restored", "change_count": restored.changeCount]
    }

    func restore() -> [String: Any] {
        if let terminal { return terminal }
        let result = performRestore()
        terminal = result
        return result
    }
}

func command(_ line: String, session: ClipboardSession) -> (receipt: [String: Any], stop: Bool) {
    guard let data = line.data(using: .utf8), let object = try? JSONSerialization.jsonObject(with: data), let value = object as? [String: Any], let operation = value["op"] as? String else {
        return (["status": "FAIL", "reason": "invalid-command"], false)
    }
    switch operation {
    case "seed":
        guard let nonce = value["nonce"] as? String else { return (["status": "FAIL", "reason": "invalid-synthetic-nonce"], false) }
        return (session.seed(nonce), false)
    case "check":
        guard let expected = value["expected"] as? String else { return (["status": "FAIL", "reason": "invalid-expected-synthetic"], false) }
        return (session.check(expected), false)
    case "restore":
        return (session.restore(), false)
    case "terminate":
        return (session.restore(), true)
    default:
        return (["status": "FAIL", "reason": "unknown-command"], false)
    }
}

struct Options {
    let board: NSPasteboard
    let timeout: TimeInterval
    let snapshotAvailable: Bool
    let failAfterClear: Bool
}

func isSyntheticPasteboardName(_ name: String) -> Bool {
    let prefix = "org.openai.pr00.clipboard."
    guard name.hasPrefix(prefix) else { return false }
    let suffix = name.dropFirst(prefix.count)
    return suffix.count == 32 && suffix.allSatisfy { "0123456789abcdef".contains($0) }
}

func parseArguments() -> Options? {
    var name: String?
    var general = false
    var timeout = 30_000
    var snapshotAvailable = true
    var failAfterClear = false
    var index = 1
    while index < CommandLine.arguments.count {
        switch CommandLine.arguments[index] {
        case "--pasteboard":
            index += 1
            guard index < CommandLine.arguments.count else { return nil }
            name = CommandLine.arguments[index]
        case "--general":
            general = true
        case "--idle-timeout-ms":
            index += 1
            guard index < CommandLine.arguments.count, let value = Int(CommandLine.arguments[index]), value >= 10, value <= 60_000 else { return nil }
            timeout = value
        case "--test-snapshot-unavailable":
            snapshotAvailable = false
        case "--test-fail-after-clear":
            failAfterClear = true
        default:
            return nil
        }
        index += 1
    }
    guard general != (name != nil), !(general && (!snapshotAvailable || failAfterClear)) else { return nil }
    if let name, !isSyntheticPasteboardName(name) { return nil }
    return Options(board: general ? NSPasteboard.general : NSPasteboard(name: NSPasteboard.Name(name!)),
                   timeout: TimeInterval(timeout) / 1000, snapshotAvailable: snapshotAvailable,
                   failAfterClear: failAfterClear)
}

func run() -> Int32 {
    guard let options = parseArguments() else {
        fputs("usage: clipboard (--pasteboard NAME | --general) [--idle-timeout-ms MILLISECONDS]\n", stderr)
        return 2
    }
    let session = ClipboardSession(options.board, snapshotAvailable: options.snapshotAvailable,
                                   failAfterClear: options.failAfterClear)
    var terminationPipe: [Int32] = [0, 0]
    guard pipe(&terminationPipe) == 0 else {
        emit(["status": "FAIL", "reason": "termination-wakeup-unavailable"])
        return 3
    }
    let terminationFlags = fcntl(terminationPipe[0], F_GETFL)
    guard terminationFlags >= 0, fcntl(terminationPipe[0], F_SETFL, terminationFlags | O_NONBLOCK) >= 0 else {
        close(terminationPipe[0]); close(terminationPipe[1])
        emit(["status": "FAIL", "reason": "termination-wakeup-unavailable"])
        return 3
    }
    terminationWriteFD = terminationPipe[1]
    signal(SIGTERM, wakeForTermination)
    defer {
        terminationWriteFD = -1
        close(terminationPipe[0]); close(terminationPipe[1])
    }
    let flags = fcntl(STDIN_FILENO, F_GETFL)
    guard flags >= 0, fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK) >= 0 else {
        emit(["status": "FAIL", "reason": "stdin-unavailable"])
        return 3
    }
    defer { _ = fcntl(STDIN_FILENO, F_SETFL, flags) }
    emit(session.readiness())
    var pending = Data()
    let deadline = Date().addingTimeInterval(options.timeout)
    while Date() < deadline {
        var signalByte: UInt8 = 0
        if Darwin.read(terminationPipe[0], &signalByte, 1) > 0 {
            emit(["event": "final", "reason": "sigterm", "restoration": session.restore()])
            return 0
        }
        var buffer = [UInt8](repeating: 0, count: 4096)
        let count = Darwin.read(STDIN_FILENO, &buffer, buffer.count)
        if count > 0 {
            pending.append(buffer, count: count)
            while let newline = pending.firstIndex(of: 10) {
                let line = String(data: pending.prefix(upTo: newline), encoding: .utf8) ?? ""
                pending.removeSubrange(...newline)
                let result = command(line, session: session)
                emit(result.receipt)
                if result.stop { return 0 }
            }
        } else if count == 0 {
            emit(["event": "final", "reason": "eof", "restoration": session.restore()])
            return 0
        } else if errno != EAGAIN && errno != EWOULDBLOCK {
            emit(["event": "final", "reason": "stdin-error", "restoration": session.restore()])
            return 3
        }
        usleep(10_000)
    }
    emit(["event": "final", "reason": "idle-timeout", "restoration": session.restore()])
    return 0
}

exit(run())
