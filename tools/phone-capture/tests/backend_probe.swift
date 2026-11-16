import Foundation
import Vision

@main
struct BackendProbe {
    @MainActor static func main() async throws {
        let client = BackendScanClient()
        client.address = CommandLine.arguments[1]
        async let first: Void = client.connect()
        async let second: Void = client.connect()
        _ = await (first, second)
        guard client.connected else { throw problem(client.status) }
        if CommandLine.arguments.contains("--catalog-only") {
            print("Connected: \(client.nodes.joined(separator:", "))")
            client.disconnect()
            return
        }
        var report: [String: Any] = ["camera_capture_tested":false, "nodes":client.nodes]
        let setup = try await client.begin(captureID:UUID().uuidString.lowercased())
        report["setup"] = setup
        let target = client.nodes.first(where:{$0 == "dortmund-slave-9"}) ?? client.nodes[0]
        var phases: [[String: Any]] = []
        do {
            let layout = setup["layout"] as! [String: Any]
            let targets = layout["nodes"] as! [[String: Any]]
            var imageChecks: [[String: Any]] = []
            for item in targets where [target,"dortmund-slave-10"].contains(item["node"] as! String) {
                let path = "http://\(CommandLine.arguments[1])/media/\(item["file"] as! String)"
                let (data, _) = try await URLSession.shared.data(from:URL(string:path)!)
                let request = VNDetectBarcodesRequest(); request.symbologies = [.qr]
                try VNImageRequestHandler(data:data, options:[:]).perform([request])
                let payloads = Set((request.results ?? []).compactMap{$0.payloadStringValue})
                let expected = Set((item["markers"] as! [[String: Any]]).map{$0["payload"] as! String})
                guard payloads == expected else { throw problem("Native Vision could not read the backend's targets") }
                imageChecks.append(["node":item["node"]!, "native_vision_targets":payloads.count])
            }
            report["target_image_checks"] = imageChecks
            for kind in ["fiducials", "white", "black", "overview"] {
                try await client.setPattern(kind, node:kind == "overview" ? nil : target)
                guard let context = client.captureContext, context["ready"] as? Bool == true,
                      let acks = context["display_acks"] as? [String: Any], acks.count == client.nodes.count else {
                    throw problem("Missing native display barrier")
                }
                phases.append(context)
            }
            report["phases"] = phases
            await client.end()
            guard client.scanSession == nil else { throw problem("Scan session did not end") }
            report["ended"] = true
        } catch {
            await client.end(); client.disconnect(); throw error
        }
        client.disconnect()
        let data = try JSONSerialization.data(withJSONObject:report, options:[.prettyPrinted,.sortedKeys])
        print(String(data:data,encoding:.utf8)!)
    }

    static func problem(_ message: String) -> NSError {
        NSError(domain:"BackendProbe",code:1,userInfo:[NSLocalizedDescriptionKey:message])
    }
}
