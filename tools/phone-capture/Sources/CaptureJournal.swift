import Foundation

// Used only on the recorder's writer queue. A frame becomes "saved" after its
// assets, frame JSON and this atomic session-index replacement all succeed.
final class CaptureJournal {
    let directory: URL
    private let metadata: [String: Any]
    private(set) var frames: [String] = []
    private(set) var errors: [String] = []
    private(set) var needsReview = 0

    init(directory: URL, metadata: [String: Any]) throws {
        self.directory = directory
        self.metadata = metadata
        try persist(status: "recording", frames: [], needsReview: 0)
    }

    func append(_ name: String, eligible: Bool) throws {
        let next = frames + [name]
        let review = needsReview + (eligible ? 0 : 1)
        try persist(status: "recording", frames: next, needsReview: review)
        frames = next
        needsReview = review
    }

    func fail(_ message: String) {
        errors.append(message)
        // Preserve the last committed index if storage itself is failing.
        // Its recording/incomplete state can never masquerade as a clean finish.
        try? persist(status: "incomplete", frames: frames, needsReview: needsReview)
    }

    func finish(writerBusyEvents: Int) throws -> Bool {
        if frames.isEmpty { errors.append("No observations recorded") }
        let complete = errors.isEmpty
        try persist(status: complete ? "complete" : "incomplete", frames: frames,
                    needsReview: needsReview, writerBusyEvents: writerBusyEvents)
        return complete
    }

    private func persist(status: String, frames: [String], needsReview: Int,
                         writerBusyEvents: Int? = nil) throws {
        var result = metadata
        result["status"] = status
        result["frames"] = frames
        result["frames_needing_review"] = needsReview
        result["errors"] = errors
        if let count = writerBusyEvents { result["writer_busy_events"] = count }
        let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
        try data.write(to: directory.appendingPathComponent("session.json"), options: .atomic)
    }
}
