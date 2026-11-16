import Foundation
import Combine

@MainActor
final class LiveAnalysisClient: NSObject, ObservableObject, @preconcurrency NetServiceBrowserDelegate, @preconcurrency NetServiceDelegate {
    @Published var address = ""
    @Published var status = "Finding live analysis on the local network…"
    @Published var instruction = "Walk slowly with the projected targets in view."
    @Published var uploaded = 0
    @Published var ready = false
    @Published var geometryReady = 0
    var onInterrupted: ((String) -> Void)?
    var onComplete: (() -> Void)?
    private let browser = NetServiceBrowser()
    private var discovering = false
    private var services: [NetService] = []
    private var socket: URLSessionWebSocketTask?
    private let transport = URLSession(configuration:.ephemeral)
    private var uploading = false
    private var sessionID: String?

    func discover() {
        guard !discovering else { return }
        discovering = true
        browser.delegate = self
        browser.searchForServices(ofType:"_immersive-scan._tcp.",inDomain:"local.")
    }
    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        services.append(service); service.delegate = self; service.resolve(withTimeout:5)
    }
    func netServiceDidResolveAddress(_ sender: NetService) {
        guard address.isEmpty, let host = sender.hostName else { return }
        address = "\(host):\(sender.port)"
        status = "Live analysis found. Ready to start."
    }

    func connect() async throws {
        guard !address.isEmpty else { throw problem("Live analysis is not found. Keep the laptop on the same Wi-Fi.") }
        var input = address.trimmingCharacters(in:.whitespacesAndNewlines)
        if !input.contains("://") { input = "ws://" + input }
        guard let endpoint = URLComponents(string:input), endpoint.scheme == "ws",
              endpoint.host != nil, endpoint.user == nil, endpoint.password == nil,
              let url = endpoint.url else { throw problem("Invalid local analysis address") }
        close(); let task = transport.webSocketTask(with:url)
        socket = task; task.resume(); status = "Connecting to live analysis…"
    }

    func begin(metadata:[String:Any]) async throws {
        let response = try await exchange(try JSONSerialization.data(withJSONObject:["cmd":"analysis_begin","metadata":metadata]))
        guard response["type"] as? String == "analysis_started",
              response["session_id"] as? String == metadata["session_id"] as? String else { throw problem("Analysis did not accept this capture") }
        sessionID = metadata["session_id"] as? String
        uploaded = 0; geometryReady = 0; ready = true
        status = "Live measurements connected"
    }

    func submit(folder:URL, name:String) {
        // One in flight, no growing network queue. Unsent frames remain in the
        // phone's complete local recording; selected frames are archived live.
        guard ready, !uploading, let identifier = sessionID else { return }
        uploading = true
        Task {
            defer { uploading = false }
            do {
                let data = try await Task.detached(priority:.utility) {
                    guard let record = try JSONSerialization.jsonObject(with:Data(contentsOf:folder.appendingPathComponent(name))) as? [String:Any] else { throw Self.problem("Invalid saved camera frame") }
                    var assets: [String:String] = [:]
                    for key in ["rgb","depth","confidence"] {
                        if let info = record[key] as? [String:Any], let filename = info["file"] as? String {
                            let bytes = try Data(contentsOf:folder.appendingPathComponent(filename))
                            guard bytes.count <= 3*1024*1024 else { throw Self.problem("Camera asset exceeds live transfer limit") }
                            assets[filename] = bytes.base64EncodedString()
                        }
                    }
                    return try JSONSerialization.data(withJSONObject:["cmd":"analysis_frame","session_id":identifier,"record":record,"assets":assets])
                }.value
                let response = try await exchange(data)
                guard sessionID == identifier else { return }
                uploaded = response["frames_stored"] as? Int ?? uploaded
                instruction = response["instruction"] as? String ?? instruction
                let nodes = response["nodes"] as? [[String:Any]] ?? []
                geometryReady = nodes.filter{$0["geometry_sufficient"] as? Bool == true}.count
                status = "\(uploaded) live measurements · \(geometryReady)/\(nodes.count) geometry checks passed"
                if response["acquisition_complete"] as? Bool == true { onComplete?() }
            } catch {
                guard sessionID == identifier else { return }
                ready = false; status = "Live analysis stopped: \(error.localizedDescription)"
                onInterrupted?(status)
            }
        }
    }

    func close() {
        ready = false; sessionID = nil
        socket?.cancel(with:.normalClosure,reason:nil); socket = nil
    }
    private func exchange(_ data:Data) async throws -> [String:Any] {
        guard let socket else { throw Self.problem("Live analysis is not connected") }
        let timeout = Task {
            do { try await Task.sleep(nanoseconds:15_000_000_000) } catch { return }
            socket.cancel(with:.goingAway,reason:nil)
        }
        defer { timeout.cancel() }
        try await socket.send(.data(data))
        let message = try await socket.receive()
        let reply: Data
        switch message {
        case .data(let bytes):reply = bytes
        case .string(let text):reply = Data(text.utf8)
        @unknown default:throw Self.problem("Unknown analysis reply")
        }
        guard let value = try JSONSerialization.jsonObject(with:reply) as? [String:Any] else { throw Self.problem("Invalid analysis reply") }
        if value["type"] as? String == "analysis_error" { throw Self.problem(value["message"] as? String ?? "Live analysis rejected a frame") }
        return value
    }
    nonisolated private static func problem(_ message:String) -> NSError {
        NSError(domain:"LiveAnalysis",code:1,userInfo:[NSLocalizedDescriptionKey:message])
    }
    private func problem(_ message:String) -> NSError { Self.problem(message) }
}
