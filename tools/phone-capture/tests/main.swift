import Foundation
import Darwin

func read(_ directory: URL) throws -> [String: Any] {
    try JSONSerialization.jsonObject(with: Data(contentsOf: directory.appendingPathComponent("session.json"))) as! [String: Any]
}

func check(_ condition: Bool, _ message: String) throws {
    if !condition { throw NSError(domain: "CaptureJournalTests", code: 1,
                                  userInfo: [NSLocalizedDescriptionKey: message]) }
}

let manager = FileManager.default
if CommandLine.arguments.count == 3 && CommandLine.arguments[1] == "abrupt" {
    let directory = URL(fileURLWithPath: CommandLine.arguments[2])
    let journal = try CaptureJournal(directory: directory, metadata: ["session_id":"abrupt"])
    try journal.append("000000.json", eligible: true)
    try journal.append("000001.json", eligible: false)
    // Simulate process termination without finish() or any cleanup callbacks.
    _exit(0)
}

let root = manager.temporaryDirectory.appendingPathComponent(UUID().uuidString, isDirectory: true)
try manager.createDirectory(at: root, withIntermediateDirectories: true)
defer { try? manager.removeItem(at: root) }
func directory(_ name: String) throws -> URL {
    let result = root.appendingPathComponent(name, isDirectory: true)
    try manager.createDirectory(at: result, withIntermediateDirectories: true)
    return result
}

let abrupt = try directory("abrupt")
let child = Process()
child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
child.arguments = ["abrupt", abrupt.path]
try child.run()
child.waitUntilExit()
try check(child.terminationStatus == 0, "Child capture failed")
let recovered = try read(abrupt)
try check(recovered["status"] as? String == "recording", "Abrupt termination looked complete")
try check(recovered["frames"] as? [String] == ["000000.json", "000001.json"], "Committed frame index lost")
try check(recovered["frames_needing_review"] as? Int == 1, "Review count lost")

let normal = try directory("normal")
let clean = try CaptureJournal(directory: normal, metadata: ["session_id":"normal"])
try clean.append("000000.json", eligible: true)
try check(try clean.finish(writerBusyEvents: 3), "Clean finish rejected")
let finished = try read(normal)
try check(finished["status"] as? String == "complete", "Finish state missing")
try check(finished["writer_busy_events"] as? Int == 3, "Admission diagnostics lost")

let interrupted = try directory("interrupted")
let partial = try CaptureJournal(directory: interrupted, metadata: [:])
try partial.append("000000.json", eligible: true)
partial.fail("tracking interrupted")
try check(try read(interrupted)["status"] as? String == "incomplete", "Interruption not persisted")
try check(try !partial.finish(writerBusyEvents: 0), "Interrupted scan accepted")
let empty = try CaptureJournal(directory: try directory("empty"), metadata: [:])
try check(try !empty.finish(writerBusyEvents: 0), "Empty scan accepted")

let failure = try directory("write-failure")
let journal = try CaptureJournal(directory: failure, metadata: [:])
try journal.append("000000.json", eligible: true)
let moved = root.appendingPathComponent("temporarily-unavailable")
try manager.moveItem(at: failure, to: moved)
var rejected = false
do { try journal.append("000001.json", eligible: false) }
catch { rejected = true }
try check(rejected, "Unavailable storage accepted a frame")
try check(journal.frames == ["000000.json"] && journal.needsReview == 0, "Failed commit advanced the saved counter")
try check(try read(moved)["frames"] as? [String] == ["000000.json"], "Previous index damaged")
try manager.moveItem(at: moved, to: failure)
try journal.append("000001.json", eligible: false)
try check(journal.frames.count == 2 && journal.needsReview == 1, "Retry did not commit")
print("CaptureJournal: abrupt process exit, clean finish, interruption, empty capture and failed-write recovery passed")

for native in [CGSize(width:1920,height:1440), CGSize(width:3840,height:2160)] {
    for (container,portrait) in [(CGSize(width:430,height:932),true),
                                 (CGSize(width:932,height:430),false),
                                 (CGSize(width:430,height:300),true)] {
        for expanded in [false,true] {
            let size=CameraFraming.previewSize(native:native,container:container,expanded:expanded,portrait:portrait)
            let aspect=portrait ? native.height/native.width : native.width/native.height
            try check(abs(size.width/size.height-aspect)<1e-9,"Preview crops the oriented sensor")
            try check(size.width<=container.width && size.height<=container.height,"Preview exceeds the viewport")
            if expanded { try check(size.height<=container.height-100,"Full camera hides its controls") }
            // Aspect-fill at matching aspect leaves all four image edges visible.
            let sensor=portrait ? CGSize(width:native.height,height:native.width) : native
            let scale=max(size.width/sensor.width,size.height/sensor.height)
            let visible=(size.width*size.height)/(sensor.width*sensor.height*scale*scale)
            try check(abs(visible-1)<1e-9,"Preview loses captured image area")
        }
    }
}
let oldVisible=(430.0*240)/(1440*1920*pow(430.0/1440,2))
try check(oldVisible<0.5,"Negative control did not reproduce the shallow-preview crop")
print("CameraFraming: portrait, landscape, keyboard viewport, full-camera controls and old-crop negative control passed")

func viewpoint(_ x: Double = 0, yaw: Double = 0) -> ScanViewpoint {
    let angle=yaw * .pi/180
    return ScanViewpoint(pose:[cos(angle),0,sin(angle),x,0,1,0,0,-sin(angle),0,cos(angle),0,0,0,0,1])!
}
var automatic=AutomaticProjectorScan()
try check(automatic.observe(node:"p1",view:viewpoint(),timestamp:0)==nil,"One sighting started a scan")
try check(automatic.observe(node:"p1",view:viewpoint(0.01),timestamp:0.5)==nil,"Two sightings started too early")
try check(automatic.observe(node:"p1",view:viewpoint(0.01),timestamp:1)=="p1","Stable detected projector did not start automatically")
automatic.complete(node:"p1",view:viewpoint(),timestamp:10)
for time in [11.0,12.0,12.5,13.0] {
    try check(automatic.observe(node:"p1",view:viewpoint(0.02),timestamp:time)==nil,"Already recorded viewpoint repeated")
}
try check(automatic.observe(node:"p1",view:viewpoint(0.5),timestamp:14)==nil,"New viewpoint triggered without settling")
_ = automatic.observe(node:"p1",view:viewpoint(0.5),timestamp:14.5)
try check(automatic.observe(node:"p1",view:viewpoint(0.5),timestamp:15)=="p1","New viewpoint could not trigger")
var moving=AutomaticProjectorScan()
for i in 0..<8 {
    try check(moving.observe(node:"p1",view:viewpoint(Double(i)*0.1),timestamp:Double(i)*0.5)==nil,"Moving phone triggered automatic references")
}
var changing=AutomaticProjectorScan()
_ = changing.observe(node:"p1",view:viewpoint(),timestamp:0)
_ = changing.observe(node:"p2",view:viewpoint(),timestamp:0.5)
try check(changing.observe(node:"p1",view:viewpoint(),timestamp:1)==nil,"Alternating projectors triggered")
try check(changing.observe(node:"p1",view:viewpoint(),timestamp:1)==nil,"Duplicate timestamp triggered")
try check(changing.observe(node:"p1",view:nil,timestamp:1.5)==nil,"Missing pose triggered")
try check(ScanViewpoint(pose:[Double.nan])==nil,"Invalid pose accepted")
var turning=AutomaticProjectorScan()
turning.complete(node:"p1",view:viewpoint(),timestamp:0)
_ = turning.observe(node:"p1",view:viewpoint(yaw:20),timestamp:3)
_ = turning.observe(node:"p1",view:viewpoint(yaw:20),timestamp:3.5)
try check(turning.observe(node:"p1",view:viewpoint(yaw:20),timestamp:4)=="p1","New viewing angle remained locked out")
print("AutomaticProjectorScan: stable trigger, movement suppression, view/angle diversity, duplicate timestamps and missing-pose checks passed")

try check(CameraSelection.choose(preferred:"ultra",available:["wide"],fallback:"wide")=="wide", "Unavailable ultra-wide blocked default camera")
try check(CameraSelection.choose(preferred:"ultra",available:["wide","ultra"],fallback:"wide")=="ultra", "Supported requested camera was lost")
try check(CameraSelection.choose(preferred:"wide",available:["lidar_virtual"],fallback:"lidar_virtual")=="lidar_virtual", "Valid ARKit virtual default was rejected")
try check(CameraSelection.choose(preferred:nil,available:[],fallback:"arkit_default")=="arkit_default", "Default format requires no manual camera selection")
print("CameraSelection: unsupported ultra-wide fallback, supported lens, virtual AR camera and default-only formats passed")
