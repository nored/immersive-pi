import Foundation
import ARKit
import Combine
import CoreImage
import CryptoKit
import ImageIO
import UniformTypeIdentifiers
import UIKit
import Vision
import AVFoundation

enum CaptureLens: String, CaseIterable, Identifiable {
    case widest = "Widest supported AR camera"
    case ultraWide = "Ultra-wide (0.5×)"
    case wide = "Wide (1×)"
    var id: String { rawValue }
}

final class CaptureRecorder: NSObject, ObservableObject, ARSessionDelegate {
    let session = ARSession()
    @Published var status = "Ready for a LiDAR-capable iPhone."
    @Published var recording = false
    @Published var finishing = false
    @Published var saved = 0
    @Published var review = 0
    @Published var export: CaptureExport?
    @Published var lens: CaptureLens = .widest
    @Published var cameraLabel = "Camera availability not checked yet."
    @Published var nativeImageSize = CGSize(width:1920, height:1440)
    @Published var ultraWideSupported = false
    var onCaptureStarted: (([String: Any]) -> Void)?
    var onCommittedFrame: ((URL, String) -> Void)?
    var onObservation: (([String: Any]) -> Void)?
    var onFailure: (() -> Void)?

    private let events = DispatchQueue(label: "immersive.capture.events")
    private let writer = DispatchQueue(label: "immersive.capture.writer")
    private let images = CIContext()
    // Session control and admission live on events; files/index live on writer.
    private var active = false
    private var busy = false
    private var previous = -Double.infinity
    private var next = 0
    private var dropped = 0
    private var folder: URL?
    private var journal: CaptureJournal?
    private var backendContext: [String: Any]?
    private var contextSinceFrame: Double?
    private var knownPayloads = Set<String>()
    private var guided = false
    private var firstFrameTime: Double?
    private var receivedDepth = false
    private var cameraInventory: [String: Any] = [:]
    private var inventorySent = false

    override init() {
        super.init()
        session.delegate = self
        session.delegateQueue = events
    }

    func refreshCameraOptions() {
        guard !recording else { return }
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
        ultraWideSupported = formats.contains { $0.captureDeviceType == .builtInUltraWideCamera }
        let cameras = Array(Set(formats.map { $0.captureDeviceType.rawValue })).sorted()
        cameraLabel = ultraWideSupported ? "Ultra-wide is exposed by ARKit on this device." : "ARKit currently exposes wide; ultra-wide is not listed for world tracking."
        cameraInventory = ["ar_world_tracking_camera_types":cameras,
            "ar_world_tracking_formats":formats.map { ["device_type":$0.captureDeviceType.rawValue,
                "width":Int($0.imageResolution.width), "height":Int($0.imageResolution.height),
                "fps":$0.framesPerSecond] as [String: Any] },
            "physical_rear_camera_types":AVCaptureDevice.DiscoverySession(
                deviceTypes:[.builtInWideAngleCamera,.builtInUltraWideCamera,.builtInTelephotoCamera],
                mediaType:.video, position:.back).devices.map { $0.deviceType.rawValue }]
    }

    private func cameraConfiguration() throws -> ARWorldTrackingConfiguration {
        guard ARWorldTrackingConfiguration.isSupported,
              ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) else {
            throw CaptureError.invalid("This device does not support ARKit LiDAR scene depth.")
        }
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        config.frameSemantics.insert(.sceneDepth)
        let formats = ARWorldTrackingConfiguration.supportedVideoFormats
        let requested: AVCaptureDevice.DeviceType?
        switch lens {
        case .widest: requested = formats.contains(where:{$0.captureDeviceType == .builtInUltraWideCamera}) ? .builtInUltraWideCamera : nil
        case .ultraWide: requested = .builtInUltraWideCamera
        case .wide: requested = .builtInWideAngleCamera
        }
        let selected = CameraSelection.choose(preferred:requested?.rawValue,
            available:formats.map{$0.captureDeviceType.rawValue},fallback:config.videoFormat.captureDeviceType.rawValue)
        if let format = formats.first(where:{$0.captureDeviceType.rawValue == selected}) {
            config.videoFormat = format
        }
        if let requested, requested.rawValue != selected { lens = .widest }
        nativeImageSize = config.videoFormat.imageResolution
        cameraLabel = config.videoFormat.captureDeviceType == .builtInUltraWideCamera
            ? "Automatic camera: ultra-wide · full sensor view"
            : "Automatic AR camera · full sensor view"
        return config
    }

    func startPreview() {
        guard !recording && !finishing else { return }
        do {
            refreshCameraOptions()
            let config = try cameraConfiguration()
            session.run(config)
            status = "Ready. Tap Start scan, then walk slowly."
        } catch { status = error.localizedDescription }
    }

    func start(captureID: String? = nil, backendSetup: [String: Any]? = nil) {
        guard !recording && !finishing else { return }
        guard ARWorldTrackingConfiguration.isSupported,
              ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) else {
            status = "This device does not support ARKit LiDAR scene depth."
            return
        }
        do {
            refreshCameraOptions()
            let identifier = captureID ?? UUID().uuidString
            guard UUID(uuidString: identifier) != nil else { throw CaptureError.invalid("Invalid capture ID") }
            let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            let target = documents.appendingPathComponent("Captures/\(identifier)", isDirectory: true)
            try FileManager.default.createDirectory(at: target, withIntermediateDirectories: true)
            let config = try cameraConfiguration()
            let format = config.videoFormat
            nativeImageSize = format.imageResolution
            cameraLabel = format.captureDeviceType == .builtInUltraWideCamera ? "Using ultra-wide · full sensor view" : "Using ARKit wide · full sensor view"
            var metadata: [String: Any] = ["format": "immersive-arkit-capture-v1", "session_id": identifier,
                "created_utc": ISO8601DateFormatter().string(from: Date()), "status": "recording",
                "device": UIDevice.current.model, "system_version": UIDevice.current.systemVersion,
                "camera_device_type": format.captureDeviceType.rawValue,
                "video_resolution": [Int(format.imageResolution.width), Int(format.imageResolution.height)],
                "video_fps": format.framesPerSecond, "world_alignment": "gravity",
                "rgb_orientation": "native_sensor_unrotated",
                "matrix_storage": "row_major", "pose_convention": "ARKit_camera_to_world_meters",
                "camera_axes": "x_right_y_up_z_back; forward_is_negative_z",
                "intrinsics_coordinates": "native_RGB_pixels",
                "depth_convention": "ARKit_sceneDepth_axial_meters",
                "frame_selection_interval_s": 0.5, "calibration_validated": false]
            metadata["camera_capabilities"] = cameraInventory
            metadata["requested_lens"] = lens.rawValue
            if let backendSetup { metadata["backend_scan"] = backendSetup }
            let layout = backendSetup?["layout"] as? [String: Any]
            let nodes = layout?["nodes"] as? [[String: Any]] ?? []
            let payloads = nodes.flatMap { ($0["markers"] as? [[String: Any]] ?? []).compactMap { $0["payload"] as? String } }
            try writer.sync {
                journal = try CaptureJournal(directory: target, metadata: metadata)
                knownPayloads = Set(payloads)
                inventorySent = false
            }
            events.sync {
                folder = target; active = true; busy = false
                previous = -Double.infinity; next = 0; dropped = 0
                backendContext = backendSetup?["context"] as? [String: Any]
                contextSinceFrame = nil
                firstFrameTime = nil; receivedDepth = false
            }
            guided = backendSetup != nil
            saved = 0; review = 0; export = nil; recording = true
            status = "Starting tracking · \(format.captureDeviceType.rawValue)"
            onCaptureStarted?(metadata)
            session.run(config, options: [.resetTracking, .removeExistingAnchors])
        } catch { status = "Cannot start capture: \(error.localizedDescription)" }
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        guard active else { return }
        if firstFrameTime == nil { firstFrameTime = frame.timestamp }
        if frame.sceneDepth != nil { receivedDepth = true }
        if !receivedDepth && frame.timestamp - (firstFrameTime ?? frame.timestamp) > 5 {
            active = false
            writer.async { self.journal?.fail("Selected camera did not provide synchronized ARKit scene depth") }
            DispatchQueue.main.async {
                self.session.pause()
                self.status = "Selected camera has no synchronized depth. Finish this capture and choose another AR camera."
                self.onFailure?()
            }
            return
        }
        if backendContext != nil && contextSinceFrame == nil { contextSinceFrame = frame.timestamp }
        guard frame.timestamp - previous >= 0.5, let target = folder else { return }
        guard !busy else { dropped += 1; return }
        busy = true; previous = frame.timestamp
        let index = next; next += 1
        let receivedWallTime = Date().timeIntervalSince1970
        let context = backendContext
        let settled = context?["ready"] as? Bool == true && frame.timestamp - (contextSinceFrame ?? frame.timestamp) >= 0.75
        writer.async {
            do {
                let (name, eligible, observation) = try self.save(frame, index: index, receivedWallTime: receivedWallTime,
                    context: context, settled: settled, to: target)
                guard let journal = self.journal else { throw CaptureError.invalid("Missing capture index") }
                try journal.append(name, eligible: eligible)
                let count = journal.frames.count, review = journal.needsReview
                DispatchQueue.main.async {
                    self.saved = count; self.review = review
                    self.status = eligible ? "Recording · tracking normal" : "Recording · observation needs review"
                    self.onCommittedFrame?(target, name)
                    if let observation { self.onObservation?(observation) }
                }
            } catch {
                self.journal?.fail("frame \(index): \(error.localizedDescription)")
                self.events.async { self.active = false }
                DispatchQueue.main.async {
                    self.status = "Write failed. Finish to export the incomplete capture."
                    self.session.pause()
                    self.onFailure?()
                }
            }
            self.events.async { self.busy = false }
        }
    }

    func updateBackendContext(_ context: [String: Any]?) {
        events.async {
            self.backendContext = context
            self.contextSinceFrame = nil
        }
    }

    func abortGuidedCapture(_ reason: String) {
        guard recording && guided else { return }
        events.sync { active = false }
        writer.async { self.journal?.fail("Backend scan interrupted: \(reason)") }
        session.pause()
        status = "Guided scan interrupted. Finish and export the partial recording."
    }

    func session(_ session: ARSession, didFailWithError error: Error) {
        guard active else { return }
        writer.async { self.journal?.fail("ARSession: \(error.localizedDescription)") }
        active = false
        DispatchQueue.main.async {
            self.status = "Tracking failed. Finish and export for diagnosis."
            self.onFailure?()
        }
    }

    func sessionWasInterrupted(_ session: ARSession) {
        // Do not join poses from a possibly changed coordinate frame silently.
        guard active else { return }
        active = false
        writer.async { self.journal?.fail("ARSession interrupted; start a new capture") }
        DispatchQueue.main.async {
            self.status = "Capture interrupted. Finish, then start a new session."
            self.onFailure?()
        }
    }

    func finish(exportArchive: Bool = true) {
        guard recording && !finishing else { return }
        finishing = true; recording = false; status = "Finishing capture…"
        session.pause()
        let state = events.sync { () -> (URL?, Int) in active = false; return (folder, dropped) }
        guard let target = state.0 else { finishing = false; return }
        writer.async {
            do {
                guard let journal = self.journal else { throw CaptureError.invalid("Missing capture index") }
                let complete = try journal.finish(writerBusyEvents: state.1)
                if !exportArchive {
                    DispatchQueue.main.async {
                        self.finishing = false
                        self.status = complete ? "Scan saved locally. Live measurements remain on the laptop." : "Partial recording retained locally."
                    }
                    return
                }
                let archive = target.deletingLastPathComponent().appendingPathComponent(target.lastPathComponent + ".zip")
                var coordinationError: NSError?
                var copyError: Error?
                NSFileCoordinator().coordinate(readingItemAt: target, options: .forUploading, error: &coordinationError) { url in
                    do { try FileManager.default.copyItem(at: url, to: archive) }
                    catch { copyError = error }
                }
                if let error = coordinationError { throw error }
                if let error = copyError { throw error }
                DispatchQueue.main.async {
                    self.finishing = false
                    self.status = complete
                        ? "Capture saved. Geometry and projector alignment still need validation."
                        : "Incomplete capture saved for diagnosis. Start a new capture before continuing."
                    self.export = CaptureExport(url: archive)
                }
            } catch {
                DispatchQueue.main.async {
                    self.finishing = false
                    self.status = "Export failed: \(error.localizedDescription). Raw files remain in Files."
                }
            }
        }
    }

    private func save(_ frame: ARFrame, index: Int, receivedWallTime: Double,
                      context: [String: Any]?, settled: Bool, to folder: URL) throws -> (String, Bool, [String: Any]?) {
        let stem = String(format: "%06d", index)
        let rgbURL = folder.appendingPathComponent(stem + ".jpg")
        let image = CIImage(cvPixelBuffer: frame.capturedImage)
        guard let rgb = images.createCGImage(image, from: image.extent),
              let destination = CGImageDestinationCreateWithURL(rgbURL as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw CaptureError.invalid("Cannot encode RGB image")
        }
        CGImageDestinationAddImage(destination, rgb, [kCGImageDestinationLossyCompressionQuality: 0.95] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw CaptureError.invalid("JPEG write failed") }
        let camera = frame.camera
        guard rgb.width == Int(camera.imageResolution.width), rgb.height == Int(camera.imageResolution.height) else {
            throw CaptureError.invalid("Encoded RGB dimensions do not match camera calibration")
        }
        let tracking: String
        switch camera.trackingState {
        case .normal: tracking = "normal"
        case .notAvailable: tracking = "not_available"
        case .limited(let reason): tracking = "limited_\(reason)"
        }
        let k = camera.intrinsics, pose = camera.transform
        var metadata: [String: Any] = ["index": index, "timestamp_s": frame.timestamp,
            "timestamp_clock": "ARFrame", "received_wall_time_unix_s": receivedWallTime,
            "tracking": tracking, "world_mapping_status": frame.worldMappingStatus.rawValue,
            "rgb": try asset(rgbURL), "image_resolution": [rgb.width, rgb.height],
            "intrinsics": (0..<3).flatMap { row in (0..<3).map { col in Double(k[col][row]) } },
            "camera_to_world": (0..<4).flatMap { row in (0..<4).map { col in Double(pose[col][row]) } },
            "exposure_duration_s": camera.exposureDuration, "exposure_offset": camera.exposureOffset]
        let luma = try lumaStats(frame.capturedImage)
        metadata["sampled_luma_statistics"] = luma
        var depthFraction = 0.0
        if let depth = frame.sceneDepth {
            let data = try packed(depth.depthMap, bytesPerPixel: 4, format: kCVPixelFormatType_DepthFloat32)
            let url = folder.appendingPathComponent(stem + "-depth.f32le")
            try data.write(to: url, options: .atomic)
            metadata["depth"] = try asset(url)
            metadata["depth_resolution"] = [CVPixelBufferGetWidth(depth.depthMap), CVPixelBufferGetHeight(depth.depthMap)]
            metadata["depth_encoding"] = "float32_little_endian_packed_rows"
            if let confidence = depth.confidenceMap {
                let bytes = try packed(confidence, bytesPerPixel: 1, format: kCVPixelFormatType_OneComponent8)
                let pixels = CVPixelBufferGetWidth(depth.depthMap) * CVPixelBufferGetHeight(depth.depthMap)
                guard bytes.count == pixels,
                      CVPixelBufferGetWidth(confidence) == CVPixelBufferGetWidth(depth.depthMap),
                      CVPixelBufferGetHeight(confidence) == CVPixelBufferGetHeight(depth.depthMap) else {
                    throw CaptureError.invalid("Confidence and depth dimensions disagree")
                }
                var valid = 0
                data.withUnsafeBytes { d in
                    bytes.withUnsafeBytes { c in
                        for i in 0..<pixels {
                            let z = d.loadUnaligned(fromByteOffset: i * 4, as: Float.self)
                            let confidence = c.load(fromByteOffset: i, as: UInt8.self)
                            if z.isFinite && z > 0 && confidence >= 1 && confidence <= 2 { valid += 1 }
                        }
                    }
                }
                depthFraction = Double(valid) / Double(max(pixels, 1))
                let url = folder.appendingPathComponent(stem + "-confidence.u8")
                try bytes.write(to: url, options: .atomic)
                metadata["confidence"] = try asset(url)
                metadata["confidence_resolution"] = [CVPixelBufferGetWidth(confidence), CVPixelBufferGetHeight(confidence)]
                metadata["confidence_encoding"] = "ARConfidenceLevel_uint8_packed_rows"
            }
        }
        metadata["valid_confident_depth_fraction"] = depthFraction
        var eligible = tracking == "normal" && luma["mean", default: 0] > 5 &&
            luma["fraction_above_5", default: 0] > 0.02 && depthFraction >= 0.1
        var observation: [String: Any]?
        if let context {
            metadata["backend_scan_context"] = context
            metadata["backend_pattern_settled"] = settled
            eligible = eligible && settled
            let kind = context["kind"] as? String ?? ""
            observation = ["session":context["session"] ?? "", "phase":context["phase"] ?? 0,
                "kind":kind, "settled":settled, "frame_index":index, "timestamp_s":frame.timestamp,
                "tracking":tracking, "eligible":eligible, "image_resolution":[rgb.width,rgb.height],
                "intrinsics":metadata["intrinsics"]!, "camera_to_world":metadata["camera_to_world"]!,
                "valid_confident_depth_fraction":depthFraction, "markers":[]]
            if eligible && ["overview", "fiducials"].contains(kind) {
                let request = VNDetectBarcodesRequest()
                request.symbologies = [.qr]
                do {
                    try VNImageRequestHandler(cvPixelBuffer:frame.capturedImage, orientation:.up, options:[:]).perform([request])
                    let markers: [[String: Any]] = (request.results ?? []).compactMap { barcode in
                        guard let payload = barcode.payloadStringValue, self.knownPayloads.contains(payload) else { return nil }
                        let corners = [barcode.topLeft, barcode.topRight, barcode.bottomRight, barcode.bottomLeft]
                            .map { [Double($0.x) * Double(rgb.width), (1 - Double($0.y)) * Double(rgb.height)] }
                        return ["payload":payload, "corners_px":corners]
                    }
                    metadata["recognized_projector_markers"] = markers
                    metadata["marker_corner_order"] = "top_left_top_right_bottom_right_bottom_left"
                    if !markers.isEmpty {
                        observation?["markers"] = markers
                        if !inventorySent {
                            observation?["camera_capabilities"] = cameraInventory
                            inventorySent = true
                        }
                    }
                } catch {
                    // Marker recognition failure does not discard camera evidence.
                    metadata["marker_detection_error"] = error.localizedDescription
                }
            }
        }
        metadata["capture_eligible"] = eligible
        metadata["eligibility_scope"] = "Basic capture checks only; no geometry or calibration accuracy claim"
        let name = stem + ".json"
        try writeJSON(metadata, folder.appendingPathComponent(name))
        return (name, eligible, observation)
    }

    private func packed(_ buffer: CVPixelBuffer, bytesPerPixel: Int, format: OSType) throws -> Data {
        guard CVPixelBufferGetPixelFormatType(buffer) == format,
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            throw CaptureError.invalid("Unsupported depth/confidence pixel format")
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(buffer) else { throw CaptureError.invalid("Missing depth buffer") }
        let rowSize = CVPixelBufferGetWidth(buffer) * bytesPerPixel
        let stride = CVPixelBufferGetBytesPerRow(buffer)
        guard stride >= rowSize else { throw CaptureError.invalid("Invalid pixel stride") }
        var result = Data(capacity: rowSize * CVPixelBufferGetHeight(buffer))
        for row in 0..<CVPixelBufferGetHeight(buffer) {
            result.append(base.advanced(by: row * stride).assumingMemoryBound(to: UInt8.self), count: rowSize)
        }
        return result
    }

    private func lumaStats(_ buffer: CVPixelBuffer) throws -> [String: Double] {
        let format = CVPixelBufferGetPixelFormatType(buffer)
        guard (format == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange ||
               format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
              CVPixelBufferGetPlaneCount(buffer) >= 1,
              CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else {
            throw CaptureError.invalid("Unsupported RGB pixel format")
        }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddressOfPlane(buffer, 0)?.assumingMemoryBound(to: UInt8.self) else {
            throw CaptureError.invalid("Missing luma plane")
        }
        let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, 0)
        var lo = 255.0, hi = 0.0, sum = 0.0, count = 0.0, bright = 0.0
        for y in Swift.stride(from: 0, to: CVPixelBufferGetHeightOfPlane(buffer, 0), by: 16) {
            for x in Swift.stride(from: 0, to: CVPixelBufferGetWidthOfPlane(buffer, 0), by: 16) {
                let raw = Double(base[y * stride + x])
                let value = format == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ?
                    min(255, max(0, (raw - 16) * 255 / 219)) : raw
                lo = min(lo, value); hi = max(hi, value); sum += value; count += 1
                if value > 5 { bright += 1 }
            }
        }
        return ["min":lo, "max":hi, "mean":sum / max(count, 1),
                "fraction_above_5":bright / max(count, 1)]
    }

    private func asset(_ url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        return ["file": url.lastPathComponent, "bytes": data.count,
                "sha256": SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()]
    }
    private func writeJSON(_ value: [String: Any], _ url: URL) throws {
        try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]).write(to: url, options: .atomic)
    }
}

enum CaptureError: LocalizedError {
    case invalid(String)
    var errorDescription: String? { if case .invalid(let message) = self { return message }; return nil }
}
