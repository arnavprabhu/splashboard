import Foundation

/// What `POST /api/admin/uninstall/plan` reports (SPEC §19, PKG-12), as the removal sheet shows it.
public struct UninstallSummary: Sendable, Equatable {
    public var home: String
    public var dataBytes: Int
    public var modelsBytes: Int
    public var cacheBytes: Int
    /// Models or cache folders outside the data folder: listed, never deleted.
    public var kept: [String]
    public var steps: [String]

    public init(home: String, dataBytes: Int, modelsBytes: Int, cacheBytes: Int, kept: [String], steps: [String]) {
        self.home = home
        self.dataBytes = dataBytes
        self.modelsBytes = modelsBytes
        self.cacheBytes = cacheBytes
        self.kept = kept
        self.steps = steps
    }

    public init(json: JSONValue) {
        func int(_ key: String) -> Int { json[key]?.int ?? 0 }
        home = json["home"]?.string ?? ""
        dataBytes = int("data_bytes")
        modelsBytes = int("models_bytes")
        cacheBytes = int("cache_bytes")
        kept = (json["items"]?.array ?? [])
            .filter { $0["deletable"]?.bool == false }
            .compactMap { $0["path"]?.string }
        steps = (json["steps"]?.array ?? []).compactMap(\.string)
    }
}

/// The user's answer in the removal sheet; nil from the host means Cancel.
public struct RemovalChoice: Sendable, Equatable {
    public var deleteModels: Bool
    public var deleteCache: Bool
    public init(deleteModels: Bool, deleteCache: Bool) {
        self.deleteModels = deleteModels
        self.deleteCache = deleteCache
    }
}
