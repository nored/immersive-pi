import Foundation
import Combine

struct ProjectorCaptureCoverage: Identifiable {
    let id: String
    let seen: Int
    let multipleViews: Int
    let total: Int
    let cells: [String: Int]
}

@MainActor
final class BackendScanClient: ObservableObject {
    @Published var address = "dortmund-control.local:8080"
    @Published var connected = false
    @Published var connecting = false
    @Published var status = "Not connected to the room."
    @Published var nodes: [String] = []
    @Published var coverage: [ProjectorCaptureCoverage] = []
    @Published var detectedNode = ""
    @Published var selectedNode = ""
    @Published var scanSession: String?
    @Published var pattern = ""
    @Published var sequenceRunning = false
    @Published var automaticScanning = true
    @Published var scanInstruction = "Start a scan, point at a projector, then hold still."
    @Published var scanningNode = ""
    @Published var referenceViews: [String: Int] = [:]

    var onContext: (([String: Any]?) -> Void)?
    var onConnectionLost: ((String) -> Void)?
    private var socket: URLSessionWebSocketTask?
    private let transport = URLSession(configuration: .ephemeral)
    private var receiveTask: Task<Void, Never>?
    private var pingTask: Task<Void, Never>?
    private var pending: [String: CheckedContinuation<[String: Any], Error>] = [:]
    private var context: [String: Any]?
    private var detectionGeneration = UUID()
    private var automatic = AutomaticProjectorScan()
    private var automaticTask: Task<Void, Never>?
    private var scanGeneration = UUID()
    private var payloadOwners: [String: String] = [:]
    private var latestView: ScanViewpoint?
    private var latestFrameTime = 0.0
    private var phaseFrames: [Int: Int] = [:]
    private var sequenceAnchor: ScanViewpoint?
    private var movedDuringSequence = false
    private var ending = false

    var targetNode: String { selectedNode.isEmpty ? detectedNode : selectedNode }
    var captureContext: [String: Any]? { context }

    func connect() async {
        guard scanSession == nil && !connecting else { return }
        connecting = true
        defer { connecting = false }
        disconnect()
        do {
            var input = address.trimmingCharacters(in: .whitespacesAndNewlines)
            if !input.contains("://") { input = "ws://" + input }
            guard var url = URLComponents(string: input), let host = url.host, !host.isEmpty else {
                throw failure("Enter the room master's hostname or IP address.")
            }
            if url.scheme == "http" { url.scheme = "ws" }
            if url.scheme == "https" { url.scheme = "wss" }
            guard ["ws", "wss"].contains(url.scheme ?? ""), url.user == nil, url.password == nil else {
                throw failure("Use the room master's local HTTP/WebSocket address.")
            }
            url.path = "/ws"; url.query = nil; url.fragment = nil
            guard let endpoint = url.url else { throw failure("Invalid room address") }
            status = "Connecting to \(host)…"
            let task = transport.webSocketTask(with: endpoint)
            socket = task; task.resume()
            receiveTask = Task { [weak self] in
                while !Task.isCancelled {
                    do {
                        let message = try await task.receive()
                        guard let self, self.socket === task else { return }
                        let data: Data
                        switch message {
                        case .string(let text): data = Data(text.utf8)
                        case .data(let value): data = value
                        @unknown default: continue
                        }
                        if let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                            self.received(value)
                        }
                    } catch {
                        guard let self, self.socket === task else { return }
                        self.lost("Room connection lost: \(error.localizedDescription)")
                        return
                    }
                }
            }
            try await send(["role":"capture", "hello":"ImmersiveCapture", "protocol":1])
            let catalog = try await request(["cmd":"capture_catalog"])
            guard catalog["protocol"] as? Int == 1 else { throw failure("Update the room backend for phone scanning.") }
            let rows = catalog["nodes"] as? [[String: Any]] ?? []
            nodes = rows.compactMap { $0["node"] as? String }.sorted { $0.localizedStandardCompare($1) == .orderedAscending }
            if !nodes.contains(selectedNode) { selectedNode = "" }
            guard !nodes.isEmpty else { throw failure("The room has no configured projectors.") }
            connected = true; status = "Connected · \(nodes.count) projectors"
        } catch { lost(error.localizedDescription) }
    }

    func begin(captureID: String) async throws -> [String: Any] {
        guard connected, scanSession == nil else { throw failure("Connect to the room before starting a guided scan.") }
        status = "Preparing native targets and waiting for projector displays…"
        let response = try await request(["cmd":"capture_begin", "capture_id":captureID])
        guard let session = response["session"] as? String, response["layout"] is [String: Any] else {
            throw failure("Backend returned an incomplete scan session.")
        }
        scanSession = session
        automatic = AutomaticProjectorScan(); referenceViews = [:]
        detectedNode = ""; latestView = nil; latestFrameTime = 0
        phaseFrames = [:]; payloadOwners = [:]; ending = false
        scanInstruction = "Point at a projector, then hold still. Scanning starts automatically."
        if let layout = response["layout"] as? [String: Any], let targets = layout["nodes"] as? [[String: Any]] {
            for target in targets {
                guard let name = target["node"] as? String else { continue }
                for marker in target["markers"] as? [[String: Any]] ?? [] {
                    if let payload = marker["payload"] as? String { payloadOwners[payload] = name }
                }
            }
        }
        pingTask?.cancel()
        pingTask = Task { [weak self] in
            while !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { return }
                guard let self, self.scanSession == session else { return }
                do { _ = try await self.request(["cmd":"capture_ping", "session":session]) }
                catch { self.lost(error.localizedDescription); return }
            }
        }
        return response
    }

    func setPattern(_ kind: String, node: String? = nil) async throws {
        guard let session = scanSession else { throw failure("No guided scan is active.") }
        if let current = context { publishContext(current.merging(["ready":false]) { _, new in new }) }
        var command: [String: Any] = ["cmd":"capture_phase", "session":session, "kind":kind]
        if let node { command["node"] = node }
        status = "Waiting for \(kind) display receipts…"
        _ = try await request(command)
    }

    func scanSelected() async {
        let target = targetNode
        guard !target.isEmpty, !sequenceRunning else { return }
        await scanProjector(target, anchor:latestView)
    }

    private func scanProjector(_ target: String, anchor: ScanViewpoint?) async {
        guard let session = scanSession, !sequenceRunning, !ending else { return }
        let generation = UUID(); scanGeneration = generation
        sequenceRunning = true
        scanningNode = target; sequenceAnchor = anchor; movedDuringSequence = false
        defer {
            if scanGeneration == generation {
                sequenceRunning = false; scanningNode = ""; sequenceAnchor = nil
            }
        }
        do {
            let steps = [("fiducials","projector targets"),("black","dark reference"),
                         ("white","bright reference"),("fiducials","final target check")]
            for (index, step) in steps.enumerated() {
                try Task.checkCancellation()
                guard scanSession == session, !ending else { return }
                let (kind,label) = step
                scanInstruction = "\(target) · \(index+1)/4 · \(label). Hold still."
                try await setPattern(kind, node:target)
                guard let phase = context?["phase"] as? Int else { throw failure("Missing scan phase") }
                // Count committed camera frames, not an assumed timer. Black
                // and white references may lack visible tracking features.
                var captured = false
                for _ in 0..<80 {
                    try Task.checkCancellation()
                    guard scanSession == session, !ending else { return }
                    if movedDuringSequence { throw failure("Phone moved during reference capture") }
                    if phaseFrames[phase,default:0] >= 2 { captured = true; break }
                    try await Task.sleep(nanoseconds: 100_000_000)
                }
                guard captured else { throw failure("Waiting for camera frames timed out") }
            }
            guard scanSession == session, !ending else { return }
            try await setPattern("overview")
            if let anchor { automatic.complete(node:target, view:anchor, timestamp:latestFrameTime) }
            referenceViews[target,default:0] += 1
            scanInstruction = "\(target): view recorded. Move to another angle or the next projector."
        } catch {
            if scanSession == session, !ending {
                try? await setPattern("overview")
                automatic.retry(timestamp:latestFrameTime)
                scanInstruction = "Hold the phone still for the reference images. We will retry automatically."
            }
        }
    }

    func observe(_ packet: [String: Any]) {
        guard packet["session"] as? String == scanSession, scanSession != nil else { return }
        guard let phase = packet["phase"] as? Int, phase == context?["phase"] as? Int else { return }
        let kind = packet["kind"] as? String ?? ""
        let settled = packet["settled"] as? Bool == true
        let eligible = packet["eligible"] as? Bool == true
        let tracking = packet["tracking"] as? String
        if let time = packet["timestamp_s"] as? Double { latestFrameTime = time }
        let view = (packet["camera_to_world"] as? [Double]).flatMap { ScanViewpoint(pose:$0) }
        if let view, tracking == "normal" {
            latestView = view
            if let anchor = sequenceAnchor, sequenceRunning,
               anchor.distance(to:view)>0.06 || anchor.angle(to:view)>5 { movedDuringSequence = true }
        }
        let markers = packet["markers"] as? [[String: Any]] ?? []
        var detected: String?
        var largest = 0.0
        var activeProjectorVisible = false
        for marker in markers {
            guard let payload = marker["payload"] as? String, let node = payloadOwners[payload],
                  let corners = marker["corners_px"] as? [[Double]], corners.count == 4,
                  corners.allSatisfy({ $0.count == 2 && $0.allSatisfy(\.isFinite) }) else { continue }
            if node == scanningNode { activeProjectorVisible = true }
            var twiceArea = 0.0
            for i in 0..<4 {
                let next = (i+1)%4
                twiceArea += corners[i][0]*corners[next][1] - corners[next][0]*corners[i][1]
            }
            let area = abs(twiceArea)/2
            if area > largest { largest = area; detected = node }
        }
        if settled && (kind == "black" || kind == "white" || (eligible && activeProjectorVisible)) {
            phaseFrames[phase,default:0] += 1
        }
        if eligible && !markers.isEmpty && ["overview","fiducials"].contains(kind) {
            Task {
                do { try await send(packet.merging(["cmd":"capture_observation"]) { _, new in new }) }
                catch { lost(error.localizedDescription) }
            }
        }
        if automaticScanning && !sequenceRunning && !ending && kind == "overview" && settled {
            let target = automatic.observe(node:eligible ? detected : nil, view:eligible ? view : nil, timestamp:latestFrameTime)
            scanInstruction = tracking == "normal" ? automatic.instruction : "Hold still while camera tracking recovers."
            if let target {
                automaticTask = Task { [weak self] in
                    guard let self else { return }
                    await self.scanProjector(target, anchor:view)
                }
            }
        }
    }

    func end() async {
        guard let session = scanSession else { return }
        ending = true; automaticTask?.cancel()
        defer { ending = false }
        do {
            _ = try await request(["cmd":"capture_end", "session":session])
            scanSession = nil; pingTask?.cancel(); publishContext(nil)
            status = "Guided capture ended. Coverage is not a validated calibration."
        } catch { lost(error.localizedDescription) }
    }

    func disconnect() {
        automaticTask?.cancel(); scanGeneration = UUID(); sequenceRunning = false; scanningNode = ""
        socket?.cancel(with: .normalClosure, reason:nil); socket = nil
        receiveTask?.cancel(); pingTask?.cancel(); connected = false
        let requests = pending; pending.removeAll()
        for continuation in requests.values { continuation.resume(throwing:failure("Room connection closed")) }
    }

    private func lost(_ message: String) {
        let wasScanning = scanSession != nil
        scanSession = nil; disconnect(); publishContext(nil); status = message
        if wasScanning { onConnectionLost?(message) }
    }

    private func publishContext(_ value: [String: Any]?) {
        context = value; pattern = value?["kind"] as? String ?? ""
        onContext?(value)
    }

    private func received(_ value: [String: Any]) {
        let type = value["type"] as? String ?? ""
        if ["capture_started", "capture_phase"].contains(type), let next = value["context"] as? [String: Any] {
            publishContext(next)
            status = sequenceRunning ? "Scan in progress — hold still" : "Ready to recognise the next projector"
        }
        if let snapshot = value["coverage"] as? [String: Any], let rows = snapshot["nodes"] as? [[String: Any]] {
            coverage = rows.compactMap { row in
                guard let node = row["node"] as? String else { return nil }
                return ProjectorCaptureCoverage(id:node, seen:row["markers_seen"] as? Int ?? 0,
                    multipleViews:row["markers_with_multiple_views"] as? Int ?? 0,
                    total:row["marker_count"] as? Int ?? 12, cells:row["cell_views"] as? [String: Int] ?? [:])
            }.sorted { $0.id.localizedStandardCompare($1.id) == .orderedAscending }
        }
        if let node = value["detected_node"] as? String {
            detectedNode = node; let generation = UUID(); detectionGeneration = generation
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if self?.detectionGeneration == generation { self?.detectedNode = "" }
            }
        }
        if type == "capture_aborted" {
            let message = value["reason"] as? String ?? "Guided capture interrupted"
            scanSession = nil; pingTask?.cancel(); publishContext(nil); status = message
            automaticTask?.cancel()
            onConnectionLost?(message)
        }
        if type == "error", value["cmd"] as? String == "capture_observation" {
            let message = value["message"] as? String ?? "needs another view"
            if !message.contains("stale") { status = "Target observation skipped: \(message)" }
        }
        if let id = value["request_id"] as? String, let continuation = pending.removeValue(forKey:id) {
            if type == "error" { continuation.resume(throwing:failure(value["message"] as? String ?? "Backend rejected the request")) }
            else { continuation.resume(returning:value) }
        }
    }

    private func send(_ value: [String: Any]) async throws {
        guard let socket else { throw failure("Connect to the room first.") }
        let data = try JSONSerialization.data(withJSONObject:value)
        try await socket.send(.string(String(decoding:data, as:UTF8.self)))
    }

    private func request(_ value: [String: Any]) async throws -> [String: Any] {
        let id = UUID().uuidString
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = continuation
            Task {
                do { try await send(value.merging(["request_id":id]) { _, new in new }) }
                catch { pending.removeValue(forKey:id)?.resume(throwing:error) }
            }
            Task {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                if let continuation = pending.removeValue(forKey:id) {
                    let message = "Room request timed out. Check the Wi-Fi and backend."
                    continuation.resume(throwing:failure(message))
                    lost(message)
                }
            }
        }
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain:"ImmersiveScan", code:1, userInfo:[NSLocalizedDescriptionKey:message])
    }
}
