# Immersive Capture

Experimental iPhone collector for room-calibration research. Records RGB,
ARKit camera intrinsics and poses, LiDAR depth and confidence. It does not
solve or apply projector calibration. See [capture instructions](../seamcal/README.md#native-phone-capture-prototype).

## Release preparation

An unsigned Release archive can be built without an Apple account:

```sh
xcodebuild -project tools/phone-capture/ImmersiveCapture.xcodeproj \
  -scheme ImmersiveCapture -configuration Release \
  -destination 'generic/platform=iOS' \
  -archivePath diagnostics/phone-capture/ImmersiveCapture.xcarchive \
  -derivedDataPath diagnostics/phone-capture-build \
  CODE_SIGNING_ALLOWED=NO archive
```

This archive is a local build check; it cannot be installed through TestFlight
without distribution signing. The project uses Developer Team `V4FUQ3TN7S`.
For distribution, sign into that team in Xcode and confirm the bundle identifier (`de.hps.immersive.capture` is
currently provisional), and create the matching App Store Connect app record.
Build a signed archive in Xcode Organizer and validate it before uploading.
Apple account login stays in Xcode; no credentials belong in the repository.

Local distribution export also works from the unsigned archive above, using
the Xcode account's automatic signing:

```sh
xcodebuild -exportArchive \
  -archivePath diagnostics/phone-capture/ImmersiveCapture.xcarchive \
  -exportPath diagnostics/phone-capture/testflight-export \
  -exportOptionsPlist tools/phone-capture/ExportOptions.plist \
  -allowProvisioningUpdates
```

`ExportOptions.plist` explicitly selects local export, not upload. Both export
options files set `testFlightInternalTestingOnly=true`, which restricts these
builds to internal TestFlight testing. `TestFlightUploadOptions.plist` selects
upload and must only be used when upload is authorized. Build 0.1.0
(1) exported successfully with Team `V4FUQ3TN7S`; the IPA's code signature and
App Store distribution provisioning profile were verified locally. Apple
server-side validation and actual on-phone capture remain unverified.

Build 0.1.0 (2), including the capture journal, was subsequently uploaded using
Xcode's TestFlight Internal Only flow. Xcode Organizer reported Uploaded, and
the operator confirmed installation through TestFlight on 2026-11-16. No public
release was submitted. Two real recordings were subsequently exported and
verified on 2026-11-16: the first had 170 observations (162 eligible), and the
static-grid room scan had 227 (224 eligible). Both included RGB, intrinsics,
poses, depth and confidence with no asset-integrity errors. Recording and ZIP
export therefore work for these sessions; device interruption handling and
capture accuracy still need validation.

Version is 0.1.0, build 7. Increment the build number for subsequent uploads.
Review App Store Connect privacy and export-compliance answers against the
actual build before submission. Guided scanning sends compact QR observations
and camera poses to the room backend. Build 6 also sends selected committed
RGB/depth/confidence assets to the operator's laptop over the local network.
Full local recordings remain in Documents as backup. No external AI service
or browser video-preview stream receives them.

Build 4 corrects the shallow portrait preview from build 3. The view now keeps
the selected sensor's oriented aspect ratio and includes a Full camera toggle;
captured RGB/depth files were already uncropped. Camera choice is based on
`ARWorldTrackingConfiguration.supportedVideoFormats`: prefer ultra-wide when
exposed, otherwise retain ARKit's default format, and display the actual lens.
An explicit unsupported ultra-wide selection is rejected. Capabilities and
the actual selected format are recorded; wide intrinsics are never reused for
another lens. A selected camera that never supplies scene depth stops the
session as incomplete rather than continuing with absent depth.

## Guided scanning with live coverage

Build 3 connects to the existing backend `/ws` endpoint. Enter
`dortmund-control.local:8080` (or the master's local IP), allow local-network
access and tap Connect. The backend and all configured render nodes need
`phone_scan_v1` support. The room must already be awake, with projectors
confirmed on by the configured room API.

Start guided capture. The backend stops playback and generates unique
session-bound QR targets at each output's native resolution. All configured
nodes must acknowledge the requested phase after a DRM scanout flip. The
phone then waits at least 750 ms of ARFrame time before accepting frames for
that phase. Transition frames remain archived but are ineligible.

The phone recognises projector targets using Apple Vision. A recognised target
names a node and a cell in its 4×3 projector grid. Live coverage distinguishes
unseen cells, cells seen from one position, and cells with observations at
least 20 cm apart according to the recorded ARKit poses. This is acquisition
coverage only: depth/pose accuracy and a usable calibration are not established.

Build 5 connects automatically when Start scan is tapped. Point at a projector
and hold still: three stable observations over at least one second start its
target → black → white → target sequence automatically. Each phase waits for
two committed, settled camera frames; QR phases also require the selected
projector to be visible. Moving over 6 cm or turning over 5 degrees during
normal tracking retries the sequence. Other projectors are black during
isolated phases. The app returns to all targets afterward. Move to another
viewpoint (at least 35 cm or 12 degrees) or another projector; already captured
views are suppressed. Manual overrides and offline recording remain available
in expandable controls. These pose thresholds are acquisition heuristics, not
validated measurement accuracy.
Finish & export ends the backend session and retains the complete raw recording.

Each saved frame records its backend session, phase, target asset checksums and
display receipts; recognised QR corners use native, unrotated RGB coordinates.
The verifier checks those receipts against the recorded layout. The backend
also preserves compact session events under `IMMERSIVE_STATE_DIR/phone-scans`
(or the media directory's `scan-state/phone-scans` fallback).

Other room commands, a node disconnect, a changed display state or an expired
phone heartbeat invalidate the scan and clear its temporary display override.
The phone stops acquisition on a lost backend session. These controls never
apply a warp, change the room model or reactivate the retired legacy scanner.
DRM receipts and projector power reports do not prove optical output; projected
QR recognition and later geometric validation remain necessary.

Before treating the build as usable, verify on a real LiDAR iPhone: camera
permission, normal tracking, saved depth/confidence, interruption handling,
Finish & export, and verification of the extracted ZIP with
`validate_capture.py`. A successful archive alone does not verify these paths.

The session index is atomically replaced after each saved observation. An app
termination before Finish leaves a `recording` index with the last committed
frames; interruption marks it `incomplete`. Neither passes as a finished scan.
These raw folders remain accessible through Files/Finder after relaunch. Do
not resume recording into an old session's coordinate frame. This protects
against process termination; it is not a guarantee against hardware/storage
failure. Empty sessions are also marked incomplete.

The Foundation-only journal tests run on macOS, including an abrupt subprocess
exit without calling Finish:

```sh
swiftc tools/phone-capture/Sources/CaptureJournal.swift \
  tools/phone-capture/Sources/CameraFraming.swift \
  tools/phone-capture/Sources/AutomaticProjectorScan.swift \
  tools/phone-capture/Sources/CameraSelection.swift \
  tools/phone-capture/tests/main.swift -o /tmp/immersive-capture-journal-tests
/tmp/immersive-capture-journal-tests
```

## Inspect captured geometry

After extracting a finished recording, export a diagnostic colored point cloud:

```sh
python3 tools/phone-capture/reconstruct_capture.py path/to/session \
  --out diagnostics/room-depth-first-scan
```

The output directory must be new and outside the capture. It contains
`scene.ply`, `trajectory.json` and `report.json`. The tool verifies capture
integrity, skips observations that fail acquisition checks, and retains only
high-confidence depth between 0.2 and 8 meters by default. This cutoff is a
filter, not a claim about LiDAR accuracy or range. Original files remain unchanged.

Depth is treated as axial distance with native RGB intrinsics scaled to the
depth resolution. Points use the recorded ARKit camera-to-world poses in
meters, with Y up. No registration or missing-surface extrapolation is applied.
Overlapping surfaces can therefore expose inconsistent recorded poses rather
than being silently forced together. The PLY can be inspected in a point-cloud
viewer; neither a plausible cloud nor its camera trajectory establishes room
coverage or pixel-accurate projector calibration.

Coordinate references: [Apple scene-depth point-cloud example](https://developer.apple.com/documentation/arkit/displaying-a-point-cloud-using-scene-depth)
and [ARCamera transform conventions](https://developer.apple.com/documentation/arkit/arcamera/transform).

Check recorded views against one another before attempting any surface fit:

```sh
python3 tools/phone-capture/check_depth_agreement.py path/to/session \
  --out diagnostics/room-depth-agreement.json
```

This samples at most 64 eligible frames, compares selected nearby views plus
the first/last pair, and projects each view's points into the other view using
the recorded poses. High-confidence target neighborhoods and both comparison
directions are required for a `consistent_coarse_depth` result. Occluded
points, depth discontinuities and missing depth do not count as agreement.
Little camera translation or inadequate overlap returns insufficient evidence.

The default 8 cm axial-depth tolerance and 80% agreement threshold are coarse
diagnostic settings, not projector-alignment tolerances. `needs_review` can
indicate inconsistent poses, changing surfaces or depth errors; it does not
identify the cause. Correlated pose/depth drift may pass this self-check.
Reports always retain `calibration_validated=false`; no poses or room settings
are changed. Use a new report path outside the original capture directory.

For a coarse vertical-surface draft and per-frame repeatability diagnostics:

```sh
python3 tools/phone-capture/fit_wall_draft.py path/to/session \
  --out diagnostics/room-wall-draft.json
```

This uses a horizontal slice around the initial camera height, fits observed
XZ line segments from even-indexed frames, and reports odd-indexed-frame
offsets without refitting them. It does not assume four walls or close gaps,
infer ceiling/floor heights, find projection boundaries or alter the room
configuration. Voxel sampling reduces the influence of repeated views of the
same patch. Explicit supported intervals retain gaps within each segment's
overall endpoint span. Default fit tolerance is 4 cm; frame associations beyond
30 cm or outside supported intervals stay unmatched. Competing surface matches
within 2 cm of one another are counted as ambiguous. Check unmatched/ambiguous
point counts and held-out errors: interleaved frames share tracking errors and cannot prove
absolute accuracy or pixel-accurate calibration.


## Build 6: live acquisition and measurement guidance

Start the laptop receiver before scanning, on the same local network:

```sh
python3 -m venv .venv-live
.venv-live/bin/pip install -r tools/phone-capture/live/requirements.txt
.venv-live/bin/python tools/phone-capture/live/server.py \
  --output diagnostics/phone-live-sessions --advertise-address YOUR_WIFI_IP
```

The phone discovers `_immersive-scan._tcp` through Bonjour. An address override
is in Advanced settings; ordinary users should only tap Start scan. The app
coordinates room patterns and sends saved measurements automatically. It
keeps only one upload in flight and retains skipped transfer frames locally.
The receiver verifies asset checksums, camera conventions, rigid poses and
saved all-projector display receipts before committing each measurement.
Its `live-index.json` explicitly represents selected frames, not a full phone
recording. `quality.json` updates continuously, including camera colour and
exposure diagnostics. Connection failure stops guided capture and preserves
the partial local recording. Cancel scan is an emergency stop; ZIP export is
not needed for live diagnosis.

The online evaluator samples high-confidence depth around QR corners, creates
world/projector correspondences, groups independent viewpoints, fits a planar
projector model on training views and checks separate held-out views. Six
spatially distinct views, all twelve cells represented in both subsets, a
half-metre baseline, held-out p95 <= 2 projector pixels and held-out plane
p95 <= 2 cm are provisional acquisition gates. Repeated stationary frames do
not count as independent evidence. Guidance appears in the camera view and
is spoken by default; reference capture also gives haptic feedback.

These gates are tested with synthetic consistent, drifted and incomplete
observations, not validated as production calibration thresholds. Nonplanar
surfaces, global loop closure, independent overlap alignment and radiometric
blending remain unresolved. The evaluator deliberately never reports full
`acquisition_complete` or `calibration_validated` yet. This is a live measurement
build, not the finished automatic calibration system. No room model is written.


Build 7 fixes the unsupported-camera startup failure observed on the operator's
phone. Camera selection is automatic; unavailable manual requests fall back to
ARKit's actual default format and its corresponding calibration. The lens picker
has been removed. The camera preview starts when the app opens, independently of
recording or laptop connection. This does not add ARKit ultra-wide support to a
device whose world-tracking formats do not expose that camera.

## Native video conversion on Apple Silicon

Install `requirements-render.txt` in a virtual environment. The experimental
`make_capture_meteor.py` converter accepts `--gpu-map --hardware-video` with
`--video`: OpenCL batches all projector remaps before reading pixels back, and
VideoToolbox handles supported source decoding and H.264 encoding. Keep the
same `--frames` value to retain the full timeline. Output stays 1280×800 per
projector; GPU selection does not reduce resolution or omit frames.

On the M3, a short 180-frame run reached about 25 scene frames per second across
12 outputs, versus about 8.5 for the earlier full render. Sustained speed for a
whole movie on the accelerated path has not been measured. GPU/native integer
rounding differed from the previous NumPy mapping by at most one RGB level;
the H.264 outputs are lossy and are not bit-identical to software encodes.

Some VideoToolbox outputs omit SPS frame timing despite correct MP4 timestamps.
The converter now adds missing timing with `repair_h264_timing.py` before
publishing the manifest. This remux verifies unchanged compressed picture data
and packet count; it does not encode pictures again. Existing VUI is preserved.
`tests/test_h264_timing.py` also checks unchanged decoded pixels and timestamps
on a small hardware-encoded sample. Final packages still need a player check
before being selected in the room.

Source dimensions do not establish projection. The downloaded KinoLet Inspire 2
drone file is a 2:1 equi-angular cubemap; use `--source-projection eac` for it.
The decoder converts its central 90-degree wall band before room sampling,
without an intermediate lossy encode. Ordinary NOAA panoramas retain the
default `--source-projection equirect`. The projection regression test checks
recovery of known spherical direction colours from a synthetic cubemap.
