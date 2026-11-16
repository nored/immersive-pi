import Foundation

// A user-selected unavailable device must never prevent camera startup. The
// default AR format is supplied by ARKit and retains its own calibration.
enum CameraSelection {
    static func choose(preferred:String?, available:[String], fallback:String) -> String {
        if let preferred, available.contains(preferred) { return preferred }
        return fallback
    }
}
