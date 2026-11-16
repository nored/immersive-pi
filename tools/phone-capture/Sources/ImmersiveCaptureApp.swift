import SwiftUI
import ARKit
import SceneKit

@main
struct ImmersiveCaptureApp: App {
    @StateObject private var recorder = CaptureRecorder()
    @StateObject private var backend = BackendScanClient()
    @StateObject private var live = LiveAnalysisClient()
    @StateObject private var guidance = ScanGuidance()
    @State private var starting = false
    @State private var cameraExpanded = false
    @State private var advanced = false
    @FocusState private var addressFocused: Bool
    var body: some Scene {
        WindowGroup {
          GeometryReader { geometry in
            let orientation = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
                .first(where:{$0.activationState == .foregroundActive})?.interfaceOrientation
            let portrait = orientation.map { $0 == .unknown ? geometry.size.height >= geometry.size.width : $0.isPortrait }
            let preview = CameraFraming.previewSize(native:recorder.nativeImageSize, container:geometry.size,
                expanded:cameraExpanded, portrait:portrait)
            VStack(spacing: 12) {
                Text("Immersive room capture").font(.headline)
                CameraPreview(session: recorder.session).frame(width:preview.width,height:preview.height).overlay(alignment: .topLeading) {
                    Text(recorder.recording ? (backend.sequenceRunning ? "Hold still while reference images are captured." : live.instruction) : recorder.status).padding(8).background(.black.opacity(0.7)).foregroundStyle(.white)
                }
                .overlay(alignment: .bottomTrailing) {
                    Button(cameraExpanded ? "Show controls" : "Full camera") { cameraExpanded.toggle(); addressFocused = false }
                        .padding(8).background(.black.opacity(0.7)).foregroundStyle(.white)
                }
                .frame(maxWidth:.infinity)
              if !cameraExpanded {
                ScrollView {
                    VStack(spacing:12) {
                        DisclosureGroup("Advanced settings", isExpanded:$advanced) {
                        Toggle("Spoken guidance",isOn:$guidance.spoken)
                        TextField("Live analysis address (usually found automatically)",text:$live.address)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                            .disabled(recorder.recording || starting)
                        HStack {
                            TextField("Room master", text:$backend.address)
                                .textInputAutocapitalization(.never).autocorrectionDisabled()
                                .textFieldStyle(.roundedBorder)
                                .focused($addressFocused).submitLabel(.go)
                                .onSubmit { addressFocused = false; Task { await backend.connect() } }
                            Button("Connect") { addressFocused = false; Task { await backend.connect() } }
                        }.disabled(recorder.recording || recorder.finishing || starting || backend.connecting)
                        Text(backend.status).font(.caption)
                        Button("Record offline without room guidance") { recorder.start() }
                            .disabled(recorder.recording || recorder.finishing || starting)
                        }
                        Text(recorder.cameraLabel).font(.caption)
                        Text(live.status).font(.caption)
                        HStack {
                            Button("Start scan") {
                                starting = true
                                addressFocused = false
                                Task {
                                    if !backend.connected { await backend.connect() }
                                    if backend.connected {
                                        let identifier = UUID().uuidString.lowercased()
                                        do {
                                            try await live.connect()
                                            let setup = try await backend.begin(captureID:identifier)
                                            recorder.start(captureID:identifier, backendSetup:setup)
                                            if !recorder.recording { await backend.end() }
                                        } catch { backend.status = error.localizedDescription; live.status = error.localizedDescription }
                                    }
                                    starting = false
                                }
                            }.disabled(recorder.recording || recorder.finishing || starting)
                            Button("Cancel scan") {
                                recorder.finish(exportArchive:false)
                                live.close()
                                Task { await backend.end() }
                            }.disabled(!recorder.recording)
                        }.buttonStyle(.borderedProminent)
                        Text("Saved: \(recorder.saved) · need review: \(recorder.review)")
                            .font(.caption).monospacedDigit()
                        if backend.scanSession != nil {
                            Text(backend.detectedNode.isEmpty ? "Aim at a target to identify its projector" : "Recognised: \(backend.detectedNode)")
                                .font(.subheadline)
                            Text(backend.sequenceRunning ? backend.scanInstruction : live.instruction).font(.subheadline).bold()
                            Text("Reference views recorded: \(backend.referenceViews.values.reduce(0,+))")
                                .font(.caption).monospacedDigit()
                            DisclosureGroup("Manual scan controls") {
                                Toggle("Automatic scanning",isOn:$backend.automaticScanning)
                                Picker("Projector",selection:$backend.selectedNode) {
                                    Text("Use recognised projector").tag("")
                                    ForEach(backend.nodes,id:\.self) { Text($0).tag($0) }
                                }
                                Button("Scan this projector") { Task { await backend.scanSelected() } }
                                    .disabled(backend.targetNode.isEmpty || backend.sequenceRunning)
                            }
                            if let row = backend.coverage.first(where:{$0.id == backend.targetNode}) {
                                Text("\(row.id): \(row.seen)/\(row.total) seen · \(row.multipleViews) with multiple viewpoints")
                                    .font(.caption)
                                LazyVGrid(columns:Array(repeating:GridItem(.flexible()),count:4),spacing:4) {
                                    ForEach(0..<row.total,id:\.self) { cell in
                                        let count = row.cells[String(cell)] ?? 0
                                        Text("\(cell/4+1)/\(cell%4+1)")
                                            .font(.caption).frame(maxWidth:.infinity).padding(8)
                                            .background((count == 0 ? Color.gray : count == 1 ? Color.orange : Color.teal).opacity(0.35))
                                            .clipShape(RoundedRectangle(cornerRadius:4))
                                    }
                                }
                                Text("Gray: unseen · orange: move to another viewpoint · teal: multiple views")
                                    .font(.caption2)
                            }
                            DisclosureGroup("Room coverage") {
                                ForEach(backend.coverage) { row in
                                    HStack {
                                        Text(row.id)
                                        Spacer()
                                        Text("\(row.multipleViews)/\(row.total)").monospacedDigit()
                                    }.font(.caption)
                                }
                            }
                        } else {
                            Text("Start scan connects to the room and shows targets. Point at a projector and hold still; its reference images are captured automatically.")
                                .font(.caption)
                        }
                        Text("Coverage is not validated calibration. No warp or blend is applied automatically.")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(.horizontal).padding(.bottom)
                }
              } else if recorder.recording {
                Text(backend.sequenceRunning ? "Hold still during reference capture." : live.instruction).font(.caption)
                Button("Cancel scan") {
                    recorder.finish(exportArchive:false); live.close(); Task { await backend.end() }
                    cameraExpanded = false
                }.buttonStyle(.borderedProminent)
              }
            }
            .onChange(of:backend.sequenceRunning) { _,running in
                if recorder.recording { guidance.referenceCapture(running) }
            }
            .onChange(of:live.instruction) { _,text in
                if recorder.recording && !backend.sequenceRunning { guidance.say(text) }
            }
            .onChange(of:recorder.recording) { _,active in
                if active { guidance.say("Walk slowly with the projected targets in view.",immediate:true) }
                else { guidance.stop() }
            }
            .sheet(item: $recorder.export) { item in ShareCapture(url: item.url) }
            .onAppear {
                recorder.startPreview()
                live.discover()
                recorder.onCaptureStarted = { metadata in
                    guard metadata["backend_scan"] != nil else { return }
                    Task { @MainActor in
                        do { try await live.begin(metadata:metadata) }
                        catch {
                            recorder.abortGuidedCapture(error.localizedDescription)
                            recorder.finish(exportArchive:false)
                            await backend.end()
                            live.status = "Cannot start live analysis: \(error.localizedDescription)"
                        }
                    }
                }
                recorder.onCommittedFrame = { folder,name in live.submit(folder:folder,name:name) }
                live.onInterrupted = { reason in
                    recorder.abortGuidedCapture(reason)
                    recorder.finish(exportArchive:false)
                    Task { await backend.end() }
                }
                live.onComplete = {
                    recorder.finish(exportArchive:false)
                    live.close()
                    Task { await backend.end() }
                }
                backend.onContext = { recorder.updateBackendContext($0) }
                backend.onConnectionLost = { reason in
                    recorder.abortGuidedCapture(reason)
                    recorder.finish(exportArchive:false)
                    live.close()
                    live.status = reason
                }
                recorder.onObservation = { packet in Task { @MainActor in backend.observe(packet) } }
                recorder.onFailure = {
                    recorder.finish(exportArchive:false)
                    live.close()
                    Task { @MainActor in await backend.end() }
                }
            }
          }
        }
    }
}

struct CameraPreview: UIViewRepresentable {
    let session: ARSession
    func makeUIView(context: Context) -> ARSCNView {
        let view = ARSCNView()
        view.session = session
        view.scene = SCNScene()
        return view
    }
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
}

struct CaptureExport: Identifiable {
    let id = UUID()
    let url: URL
}

struct ShareCapture: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: [url], applicationActivities: nil)
    }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
