# Room media library

`test-media/media-library/catalog.json` records curated downloadable 360° sources,
creator credits and reuse terms. It includes actual drone footage separately from
paragliding and aircraft footage. Downloads remain inside the workspace.

Index existing source files:

```sh
python3 tools/media-library/collect.py test-media/media-library/catalog.json --output test-media
```

Add `--fetch` to download missing entries. The collector resumes its own partial
files, checks available publisher checksums, probes actual dimensions and records
source hashes in `test-media/index.json`. A downloaded source is explicitly not a
ready room package. Standard flat videos and unsupported panorama layouts need
separate review; they are not stretched around the room.

For the current experimental scan-derived mapping, render with
`tools/phone-capture/make_capture_meteor.py --video SOURCE --models MODELS
--baseline ROOM_MODEL --output NEW_DIRECTORY --frames FRAME_COUNT
--hardware-video --gpu-map`. Native outputs stay 1280×800. `--hardware-video`
selects Apple video encoding and supported hardware source decoding; `--gpu-map`
uses OpenCL for mapping and blending. A CPU-only fallback uses OpenCV's native
multiply operation rather than repeated NumPy float conversions.

`publish.py` uploads clips, pre-caches and hash-verifies the corresponding clip
on each node, then publishes the manifest. No new HTTP endpoint, calibration edit
or service restart is involved. `--replace-current OLD_MANIFEST` only switches
playback if that exact show is still playing; it preserves other operator choices.
Source credit and license metadata should be included in published manifests and
kept with every derivative package. These room mappings remain experimental.

Convert selected full picture tracks in sequence on the laptop:

```sh
python tools/media-library/convert.py test-media/media-library/catalog.json \
  --source-root test-media \
  --models test-media/mapping/experimental-dortmund.models.json \
  --baseline test-media/mapping/experimental-dortmund.baseline.json \
  --output NEW_OUTPUT_DIRECTORY \
  --ids paragliding-tolmin-5760 noaa-sea-lion nasa-alaska-glaciers-5120
```

The batch uses video-track duration so a longer audio track cannot create
requests for nonexistent pictures. It writes a progress report, per-source logs
and attributed native manifests. Conversion completion does not imply player
validation or publication. Validate all twelve clips with the actual player,
then publish each package. The drone source is EAC cubemap packed into a 2:1
frame; its explicit catalog projection selects conversion before room mapping.
Dimensions alone cannot establish a source's projection.

The committed `test-media/mapping/experimental-dortmund.*.json` snapshots reproduce
this session's baked test mapping. They contain recovered parameters from capture
`8c303c1a-c0bf-4c25-a858-c0d1a69cf6dd` and the existing version-34 room baseline.
Their held-out errors exceed the production target. Use them for media conversion
experiments; they are not a validated automatic-calibration result to install.
