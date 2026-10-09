import Foundation
import IOKit

/// GPU utilization for the `menubar.show_gpu` gauge: IOKit `IOAccelerator`
/// → `PerformanceStatistics` → `"Device Utilization %"`, readable without sudo.
/// Returns nil when no accelerator publishes the key; the gauge is then hidden.
public protocol GPUSampling: Sendable {
    func utilization() -> Int?
}

public struct IOKitGPUSampler: GPUSampling {
    public static let statisticsKey = "PerformanceStatistics"
    public static let utilizationKey = "Device Utilization %"

    public init() {}

    public func utilization() -> Int? {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching("IOAccelerator"), &iterator)
            == KERN_SUCCESS
        else { return nil }
        defer { IOObjectRelease(iterator) }
        var best: Int?
        while case let service = IOIteratorNext(iterator), service != 0 {
            defer { IOObjectRelease(service) }
            var props: Unmanaged<CFMutableDictionary>?
            guard IORegistryEntryCreateCFProperties(service, &props, kCFAllocatorDefault, 0) == KERN_SUCCESS,
                  let dict = props?.takeRetainedValue() as? [String: Any]
            else { continue }
            if let value = Self.extract(from: dict) {
                best = max(best ?? 0, value)
            }
        }
        return best
    }

    /// Pulls the utilization out of an accelerator's registry properties.
    public static func extract(from properties: [String: Any]) -> Int? {
        guard let stats = properties[statisticsKey] as? [String: Any] else { return nil }
        let raw = stats[utilizationKey]
        let value: Int?
        switch raw {
        case let n as NSNumber: value = n.intValue
        case let i as Int: value = i
        default: value = nil
        }
        return value.map { min(100, max(0, $0)) }
    }
}
