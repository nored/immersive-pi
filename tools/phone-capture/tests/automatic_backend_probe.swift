import Foundation

@main
struct AutomaticBackendProbe {
    @MainActor static func main() async throws {
        let client = BackendScanClient()
        client.address = CommandLine.arguments[1]
        await client.connect()
        try require(client.connected, "Connection failed")
        _ = try await client.begin(captureID:UUID().uuidString.lowercased())
        var time = 0.0
        func frame(markers: Bool = true, settled: Bool = true) {
            guard let context = client.captureContext else { return }
            time += 0.5
            let pose: [Double] = [1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]
            client.observe(["session":client.scanSession!, "phase":context["phase"]!,
                "kind":context["kind"]!, "settled":settled, "eligible":markers,
                "tracking":"normal", "timestamp_s":time, "camera_to_world":pose,
                "markers":markers ? [["payload":"target1", "corners_px":[[10.0,10.0],[100,10],[100,100],[10,100]]]] : []])
        }
        for _ in 0..<3 { frame(); try await Task.sleep(nanoseconds:20_000_000) }
        for expected in ["fiducials", "black", "white", "fiducials"] {
            for _ in 0..<100 {
                if client.captureContext?["kind"] as? String == expected { break }
                try await Task.sleep(nanoseconds:20_000_000)
            }
            try require(client.captureContext?["kind"] as? String == expected, "Wrong phase: \(expected)")
            let phase = client.captureContext?["phase"] as! Int
            frame(settled:false)
            try await Task.sleep(nanoseconds:150_000_000)
            try require(client.captureContext?["phase"] as? Int == phase, "Unsettled frame advanced phase")
            frame(markers:expected == "fiducials")
            try await Task.sleep(nanoseconds:150_000_000)
            try require(client.captureContext?["phase"] as? Int == phase, "One frame advanced phase")
            frame(markers:expected == "fiducials")
            try await Task.sleep(nanoseconds:150_000_000)
        }
        try require(client.referenceViews["p1"] == 1, "Reference sequence not recorded")
        try require(client.pattern == "overview", "Overview not restored")
        for _ in 0..<8 { frame(); try await Task.sleep(nanoseconds:20_000_000) }
        try require(!client.sequenceRunning, "Repeated unchanged viewpoint")
        await client.end()
        try require(client.scanSession == nil, "Session did not end")
        _ = try await client.begin(captureID:UUID().uuidString.lowercased())
        for _ in 0..<3 { frame(); try await Task.sleep(nanoseconds:20_000_000) }
        try require(client.sequenceRunning, "Second session did not begin automatic sequence")
        await client.end()
        try await Task.sleep(nanoseconds:250_000_000)
        try require(client.scanSession == nil && !client.sequenceRunning && client.pattern.isEmpty,
                    "Ending during sequence left capture running")
        client.disconnect()
        print("Automatic backend integration: stable recognition, ordered QR/black/white/QR, settled two-frame barriers, dark frames, repeat suppression and cancellation during capture passed")
    }
    static func require(_ condition: Bool, _ message: String) throws {
        if !condition { throw NSError(domain:"AutomaticProbe",code:1,userInfo:[NSLocalizedDescriptionKey:message]) }
    }
}
