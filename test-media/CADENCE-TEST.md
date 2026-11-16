# Native display cadence experiment

The installed 1280×800 mode uses an 83.5 MHz pixel clock and totals of
1680×831 pixels: 59.8103260558 refreshes per second. The `60` label is rounded.
Two refreshes per video frame therefore correspond to `521875/17451` fps.

Create a separate diagnostic package, preserving frame count and tile regions:

```sh
python3 tools/retime_test_package.py \
  test-media/native-ring-1280/native-ring-1280x800.immersive.json \
  test-media/native-ring-display-matched --fps 521875/17451
```

This re-encodes H.264 clips and checks each result with ffprobe. It changes the
720-frame loop duration from 24 seconds to approximately 24.07611 seconds. The
original clips remain intact. Upload every output clip before its manifest.
This rate is specific to the measured mode; do not automatically retime user
content or change its playback speed.

The master can record every node's DRM presentation acknowledgement with the
opt-in `IMMERSIVE_TRACE_PATH` environment variable. Use a RAM-backed path under
`/run`, stop playback before restarting the controller, and remove the temporary
environment override after the experiment. Compare both rates under the same
trace configuration; tracing itself adds overhead.

```sh
python3 tools/analyze_display_cadence.py master-trace.jsonl \
  --refresh-hz 59.81032605581342 --out cadence.json
```

The analyzer separates sessions, counts only consecutive frame intervals, and
reports missing sequences. Intervals within 1 ms of an integer refresh count
are classified as one, two, three, etc.; others remain unclassified. A matching
30-ish fps clip should last two refreshes per frame. Zero protocol holds alone
does not prove this. DRM timing also does not establish projector optical timing
or synchronize the physical scanout phases of separate HDMI outputs.

Renderer traces additionally include `flip_submitted` events when tracing is
enabled. These contain the selected submission window and timestamps before
and after the native queue call. Inspect them with:

```sh
python3 tools/analyze_submission_trace.py renderer-trace.jsonl --out submissions.json
```

For each irregular interval, compare `chosen_refreshes` with `actual_refreshes`
and inspect `return_margin_ms`. A matching choice with substantial margin points
to refresh selection rather than a late native submission. Initial unmeasured
flips are excluded. The presentation and selection timestamps use sampled clock
conversions, so isolated large errors still require care when interpreting them.

The presenter now converts each flip once and passes the same result to the
acknowledgement path. `flip_clock` events retain the raw local DRM timestamp,
converted timestamp and sampling span. Clock reads are bracketed by local reads;
spans above 0.2 ms trigger up to three attempts. A recent better offset can be
reused for up to one second if all attempts are interrupted. Initial or stale
samples use the best available attempt, with its uncertainty recorded.

The analyzer reports raw DRM refresh intervals independently of master-clock
conversion. Its `max_sampling_uncertainty_ns` describes the read bracket only;
it does not bound network-clock accuracy, HDMI phase differences or projector
latency. A short bracket alone cannot prove physically synchronized projection.

At the first commit, the presenter queries `DRM_IOCTL_CRTC_GET_SEQUENCE` on the
active CRTC and converts that current refresh timestamp. This seeds frame-zero
scheduling instead of waiting for the first video flip to reveal display phase.
`phase_seed` records the measurement and age. Unsupported/inactive queries or
timestamps older than two refreshes retain the unmeasured initial fallback.
No display mode change or additional DRM event is requested by this query.
