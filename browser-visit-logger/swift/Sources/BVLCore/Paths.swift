import Foundation

/// Path constants — read once from the environment at startup so callers
/// can reference them without re-reading.  Mirrors the Python side's
/// BVL_* env var convention so existing tooling and tests can override
/// paths the same way.
public enum Paths {

    /// Re-read at every property access so unit tests can mutate the
    /// environment between cases without re-launching the process.
    private static func env(_ key: String, _ fallback: @autoclosure () -> String) -> String {
        ProcessInfo.processInfo.environment[key] ?? fallback()
    }

    public static var home: String {
        NSHomeDirectory()
    }

    public static var logDir: String {
        env("BVL_LOG_DIR", home)
    }

    public static var dbFile: String {
        env("BVL_DB_FILE", (home as NSString).appendingPathComponent("browser-visits.db"))
    }

    public static var hostLog: String {
        env("BVL_HOST_LOG",
            (home as NSString).appendingPathComponent("browser-visits-host.log"))
    }

    public static var downloadsSnapshotsDir: String {
        env("BVL_DOWNLOADS_SNAPSHOTS_DIR",
            (home as NSString)
                .appendingPathComponent("Downloads/browser-visit-snapshots"))
    }

    /// Archive root for read/skimmed snapshots.  Historically this
    /// pointed at an iCloud-backed Documents subdir; the multi-laptop
    /// architecture moves it to a Google Drive folder accessible from
    /// both the laptops and the EC2 VM.  `BVL_GDRIVE_SNAPSHOTS_DIR` is
    /// the preferred env var; `BVL_ICLOUD_SNAPSHOTS_DIR` is honored as
    /// an alias for backwards compatibility with existing installs and
    /// tests.
    public static var archiveSnapshotsDir: String {
        if let v = ProcessInfo.processInfo.environment["BVL_GDRIVE_SNAPSHOTS_DIR"] {
            return v
        }
        if let v = ProcessInfo.processInfo.environment["BVL_ICLOUD_SNAPSHOTS_DIR"] {
            return v
        }
        return (home as NSString).appendingPathComponent(
            "Library/CloudStorage/GoogleDrive/My Drive/browser-visit-logger/snapshots")
    }

    /// Deprecated alias; new code should call `archiveSnapshotsDir`.
    /// Retained because external Swift callers may still reference it
    /// during the transition.
    public static var icloudSnapshotsDir: String { archiveSnapshotsDir }

    public static var moverErrorThreshold: Int {
        Int(env("BVL_MOVER_ERROR_THRESHOLD", "3")) ?? 3
    }

    /// Per-laptop config directory (machine ID, mTLS cert/key, sync
    /// endpoint config).  Kept out of `home` directly so it survives
    /// reinstalls and is easy to back up.
    public static var configDir: String {
        env("BVL_CONFIG_DIR",
            (home as NSString).appendingPathComponent(".browser-visit-logger"))
    }

    /// JSON file holding the laptop's machine identity (first observed
    /// hostname, ISO timestamp it was assigned).  Touched once at first
    /// run, read every invocation.
    public static var machineConfigFile: String {
        (configDir as NSString).appendingPathComponent("config.json")
    }

    /// Local mirror of peer machines' log files, populated by BVLSync
    /// on pull.  Mirrors the VM's `<vm-log-root>/<machine_id>/` layout.
    public static var peerLogsDir: String {
        env("BVL_PEER_LOGS_DIR",
            (home as NSString).appendingPathComponent("browser-visits-peers"))
    }

    /// Machine identifier embedded in this laptop's log filenames and
    /// sent on every gRPC sync RPC.  Resolution order:
    ///   1. `BVL_MACHINE_ID` env var (for tests + recovery).
    ///   2. `machine_id` field in `machineConfigFile`.
    ///   3. macOS `LocalHostName` (sanitised), persisted to the config
    ///      file on first run.
    public static func machineId() -> String {
        if let env = ProcessInfo.processInfo.environment["BVL_MACHINE_ID"],
           !env.isEmpty {
            return sanitiseMachineId(env)
        }
        if let fromConfig = readMachineIdFromConfig() {
            return fromConfig
        }
        let host = currentHostName()
        let sanitised = sanitiseMachineId(host)
        // Persist on first observation so a later rename doesn't split
        // identity.  Failure to persist is non-fatal — we'll re-derive
        // from hostname next time, and the user will see a warning in
        // the host log if hostnames diverge.
        try? persistMachineId(sanitised)
        return sanitised
    }

    private static func currentHostName() -> String {
        // ProcessInfo.processInfo.hostName has been observed to return
        // ".local"-suffixed values that depend on DHCP; LocalHostName
        // via Host.current() is the user-visible Sharing name.  Fall
        // back to gethostname if both are empty.
        let names = Host.current().localizedName ?? Host.current().name ?? ""
        if !names.isEmpty {
            return names
        }
        let info = ProcessInfo.processInfo.hostName
        if !info.isEmpty { return info }
        return "unknown-host"
    }

    private static func sanitiseMachineId(_ raw: String) -> String {
        let allowed = Set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        let mapped = raw.map { allowed.contains($0) ? $0 : "-" }
        let collapsed = String(mapped).replacingOccurrences(
            of: "--", with: "-", options: .regularExpression)
        let trimmed = collapsed.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        return trimmed.isEmpty ? "unknown-host" : trimmed
    }

    private static func readMachineIdFromConfig() -> String? {
        let path = machineConfigFile
        guard let data = FileManager.default.contents(atPath: path) else {
            return nil
        }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let id = obj["machine_id"] as? String,
              !id.isEmpty
        else { return nil }
        return sanitiseMachineId(id)
    }

    private static func persistMachineId(_ id: String) throws {
        try FileManager.default.createDirectory(
            atPath: configDir, withIntermediateDirectories: true)
        let fm = ISO8601DateFormatter()
        let payload: [String: Any] = [
            "machine_id": id,
            "assigned_at": fm.string(from: Date()),
        ]
        let data = try JSONSerialization.data(
            withJSONObject: payload, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: URL(fileURLWithPath: machineConfigFile))
    }

    /// Per-day visit log filename for a UTC date, e.g.
    /// "browser-visits-2026-04-30.log".  The laptop's own logs keep
    /// the historical (unprefixed) filename because the machine ID is
    /// implicit on disk.  Cross-machine storage (VM mirror, local peer
    /// mirror) encodes the machine ID in the parent directory instead.
    public static func logFilename(for dateISO: String) -> String {
        "browser-visits-\(dateISO).log"
    }

    /// Absolute path of the per-day visit log inside `logDir`.
    public static func logPath(for dateISO: String) -> String {
        (logDir as NSString).appendingPathComponent(logFilename(for: dateISO))
    }

    /// Absolute path of a peer machine's day log in the local mirror.
    /// Layout matches the VM side: `<peerLogsDir>/<machine_id>/<filename>`.
    public static func peerLogPath(machineId: String, dateISO: String) -> String {
        let mid = sanitiseMachineId(machineId)
        let dir = (peerLogsDir as NSString).appendingPathComponent(mid)
        return (dir as NSString).appendingPathComponent(logFilename(for: dateISO))
    }

    /// Marker file written when we can't reach Notification Center.
    public static var attentionFile: String {
        (home as NSString).appendingPathComponent("browser-visits-mover-needs-attention")
    }
}
