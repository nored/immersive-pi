# Meteor fly-by panorama

`meteor-flyby-360.mp4` is the clean animation; `meteor-flyby-360-diagnostic.mp4`
adds 12 sector markers and identical frame counters in every sector.
Both are 1920×1080, 30 fps, 30 seconds, silent H.264/yuv420p MP4 files.

Upload either file through the controller's media interface and select it for
room playback. Keep the existing equal horizontal source regions: each node
shows its portion of the same panorama. This is 1080p for the whole panorama,
not 1080p per projector. The preview looks horizontally compressed on a monitor;
it is intended to wrap around all four walls. Existing player synchronization
still determines whether frames actually appear together on the projectors.

The scene uses cylindrical 360° projection, a stationary star field and 38
procedural low-poly rocks following looping near-miss trajectories. The visual
motion loops; diagnostic frame numbers reset at the loop. The left/right seam
wraps correctly. There is no spacecraft interior or audio.

Regenerate with Python, numpy, Pillow and ffmpeg:

```sh
python3 test-media/make_meteor_clip.py
# Change labels for another room layout:
python3 test-media/make_meteor_clip.py --nodes 8 --out test-media/meteor-8.mp4
```

This is a deterministic mesh render, with no generated image assets.

For native per-projector detail, generate a tiled package:

```sh
python3 test-media/make_native_meteor.py
# Another node count or a diagnostic overlay:
python3 test-media/make_native_meteor.py --nodes 8 --diagnostic --fps 30 \
  --outdir test-media/native-meteor-8
```

The default package has twelve separate 1280×800 H.264 clips and a
`native-meteor-1280x800.immersive.json` manifest. Upload every clip first, then
the manifest, and select the manifest for playback. Each clip contains 720
frames. The default rate, `521875/17451` fps, matches two refreshes of Dortmund's
measured 1280×800 HDMI timing; the loop lasts approximately 24.076 seconds.
Use `--fps 30` for a true 60 Hz output. This generated diagnostic rate does not
change the playback speed of other uploaded media.

All tiles come from one cylindrical scene with matching angular pixel scale
and a continuous 360° seam. The clean package has no labels; `--diagnostic`
adds hostnames and frame numbers. Encoding verifies each clip's dimensions,
frame count and exact rational rate before writing the manifest. Source sectors
are disjoint; physical overlap coverage and edge blending still need calibration.

For a room model with measured overlapping source regions, generate matching
clips without changing the room:

```sh
python3 test-media/make_native_meteor.py --room-model path/to/room-model.json \
  --outdir test-media/native-meteor-mapped
```

This preserves the model's node IDs and writes a `-mapped.immersive.json`
manifest. Every clip remains 1280×800 by default. The source panorama is sized
to supply at least one source pixel per output pixel, then each tile samples
its declared region. Wrapped regions include the neighboring content across
the 360° seam. `--nodes`, if supplied, must match the model's node count.
Use the same source regions when playing this package. The command does not
apply geometry, blend settings, or any calibration to the live room.

The corrected `make_ring_model.py --overlap 0.08` means 8% of **each projector's
width** shared with its neighbor. Older generator versions mixed panorama and
projector units. Regenerate a draft and measure the physical overlap before
adopting it; existing disjoint clips cannot supply pixels outside their tiles.


## Scan-derived native test clips

`tools/phone-capture/make_capture_meteor.py` can bake a draft recovered room
mapping into twelve (or N) native clips without editing the live room model.
Each projector samples the same cylindrical scene through its measured planar
homography. Estimated coverage beyond QR observations and provisional overlap
weights are included. These are experimental tests, not validated calibration.

Use `--flight --frames 720` for observer translation through a stationary,
layered asteroid field: all objects share one camera motion, the horizon stays
fixed, and the approximately 24-second course loops continuously. Without
`--flight`, the observer is stationary and individual asteroids pass by.
The command requires the recovered `draft-models.json`, the matching baseline
room model and a new output directory. It checks output dimensions, rational
frame rate and frame count before writing its manifest. Upload all generated
MP4 files followed by that manifest; every node remains at 1280×800.


Use `--fall --frames 720` for a fast downward observer course. Nearby asteroid
positions rise together while distant stars rise more slowly according to depth;
stars also twinkle gently. The horizon remains fixed. The field wraps outside
visible regions to produce a continuous approximately 24-second loop. This mode
uses the same scan-derived wall mapping and provisional overlap weights.


Use `--plunge --frames 720` for the stronger enclosing-chasm version: textured
nearby rock walls fill the peripheral view, with close debris, upward particle
trails and a periodic acceleration curve. It retains the fixed horizon and
scan-derived native mapping. The scene is a visual motion test; physical
immersion remains an in-room subjective assessment.


The same mapper accepts `--video path/to/equirectangular.mp4 --frames 900` for
real 2:1 monoscopic 360° footage. It samples spherical latitude from each
reconstructed wall point, rather than stretching a flat image around the room.
The central latitude band is decoded at source resolution, and every output
remains 1280×800. Source attribution and download-integrity records for the NOAA
kelp/fish example are in `underwater-source/SOURCE.json`. The roughly 30-second
excerpt loops as recorded footage; its start/end are not claimed seamless.
