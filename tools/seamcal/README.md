# Camera calibration candidates

**Experimental research code. The existing scan results are not a calibrated
reference for this room.** The older `control-node/autocalib` scanner is retired
and must not be used. Keep raw images as evidence, not its fitted output.

The intended replacement must work from guided phone scans without ruler
measurements or manually surveyed projector positions. It is not implemented
yet. Acquisition, camera/view registration, projection-footprint detection,
coarse geometry, independent validation, and photometric blending need separate
stages. Preserve original recordings and their capture metadata for replay.

### Native phone capture prototype

The independent project at `tools/phone-capture/ImmersiveCapture.xcodeproj`
records ARKit observations on a LiDAR-capable iPhone. Open it in Xcode, choose
your Personal Team under Signing, select the connected iPhone and run it.
Device-side Developer Mode and camera permission are required. An unsigned
iPhone build passes locally. The operator confirmed installation through
TestFlight on 2026-11-16. Two exported recordings passed asset verification;
their tracking and geometry remain research data, not an accepted calibration.
Free Personal Team signing expires after seven days.

Use a static projected pattern during acquisition: moving video can confuse
visual tracking. The collector does not switch the projectors to a pattern.
Start capture, walk slowly and translate between views of each wall/corner,
then finish and export the ZIP. The original session folder remains under the
app's Documents/Captures directory, accessible through Files/Finder. Each
selected frame includes an unrotated native RGB JPEG, its pixel intrinsics,
ARKit camera-to-world pose, timestamp, exposure and tracking state, plus packed
LiDAR float32 depth and uint8 confidence maps when available. Asset hashes and
explicit coordinate conventions accompany the data. Sensor processing uses
ARKit's fused visual/inertial tracking; raw accelerometer/gyro logs are not
recorded separately. The app records its actual ARKit camera format; it does
not assume earlier ultra-wide videos share that calibration.

The writer keeps at most one frame pending and records at most two frames per
second. Dark frames, limited tracking and missing depth remain archived and
are marked for review. Session interruption stops acquisition; start a new
session instead of silently combining coordinate frames. The frame index is
saved atomically after each observation, so a terminated app leaves an indexed
partial recording in Files/Finder, explicitly not a cleanly finished scan.
Build 3 adds [backend-controlled QR targets, isolated reference sequences and
live capture coverage](../phone-capture/README.md#guided-scanning-with-live-coverage).
These are acquisition tools; geometry solving and calibration application are
not implemented in the phone app. They do not reuse the old scanlib or seamcal
fitting procedures.

After extracting the archive, inspect the session directory with:

```sh
python3 tools/phone-capture/validate_capture.py path/to/extracted/session
```

Requires NumPy and Pillow. The verifier checks asset hashes, native image
dimensions, matrix conventions, rigid poses, chronological frame records,
tracking, dark images and depth/confidence consistency. Passing these checks
does not prove room coverage, pose accuracy, correct projection boundaries or
pixel-accurate alignment. The output always states `calibration_validated=false`.

The upper projection boundary is not the ceiling, and the lower boundary is
not automatically the floor. Current `border_lines()` substitutes wall/global
medians for missing measurements, extrapolates to the wall top, and uses those
estimates to drive vertical warps. `solve_wall.py` also overwrites its fitted
vertical model using those borders. These are experimental assumptions, not
validated measurements; do not reuse them as geometry ground truth.

Recorded failure cases include the door/curtain corner around nodes 9/10,
overprinted colored markers, curtain folds, foreground occlusions, and missing
or black captures. A fitting residual on training pairs is insufficient:
evaluation must include independent views, boundary uncertainty, mesh stability
and a connected, consistent full-room model. Do not silently fill unknown
boundaries or compensate for bad measurements with a finer, wavy mesh.

Capture and solve produce a draft. Both Try and Apply validate the entire draft
before sending any live edits: every selected node and neighbour seam must be
present, mesh triangles must stay unfolded, and each seam's 95th-percentile
residual must be at most 4 pixels in the solver's reference coordinate system.
An inaccurate draft remains available for inspection and further capture.

Blend gamma now describes the projector response (typically 2.2). The renderer
applies its inverse to the light weights. Captures solved with the older blend
convention must be solved again before Try or Apply. Existing room calibration
is not migrated automatically.

Physical overlap also requires matching content in adjacent tiles. Changing
source regions beyond the coverage of a prepared tile does not generate that
missing content; regenerate overlapping tiles from the original panorama.
