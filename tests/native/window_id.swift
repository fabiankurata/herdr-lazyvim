import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 2, let pid = Int32(CommandLine.arguments[1]), pid > 0 else {
    fputs("usage: window-id PID\n", stderr)
    exit(2)
}
guard let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else {
    fputs("CGWindowListCopyWindowInfo returned nil\n", stderr)
    exit(3)
}
let windows = rows.filter {
    ($0[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid
}.map { row -> [String: Any] in
    ["pid": pid, "window_id": row[kCGWindowNumber as String] ?? NSNull(),
     "bounds": row[kCGWindowBounds as String] ?? NSNull(),
     "layer": row[kCGWindowLayer as String] ?? NSNull()]
}
let data = try JSONSerialization.data(withJSONObject: [
    "screen_capture_preflight": CGPreflightScreenCaptureAccess(), "windows": windows
], options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
