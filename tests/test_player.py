import subprocess
import time

import pytest


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "1080p-test.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1920x1080:rate=30:duration=1", "-c:v", "libx264",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30", str(path)], check=True)
    return path


def test_full_hd_decode_is_sequential_across_loops_and_stops_without_deadlock(video):
    from player import Player
    player = Player(decoder="avdec_h264", buffer_frames=3)
    try:
        spec = player.prepare(str(video))
        assert spec["width"] == 1920 and spec["height"] == 1080
        assert spec["frames"] == 30
        assert player.buffered_range() == (0, 2)  # ready includes the decode reserve
        deadline = time.monotonic() + 10
        loop_edges = {}
        for sequence in range(65):
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = player.take_frame(sequence)
                if frame is None:
                    time.sleep(.001)
            assert player.error is None
            assert frame is not None, f"missing frame {sequence}"
            assert frame.sequence == sequence
            assert abs(frame.pts - (sequence % 30) * 1_000_000_000 // 30) < 1000
            assert len(frame.planes[0]) == 1920 * 1080
            assert len(frame.planes[1]) == 1920 * 540
            if sequence % 30 in (0, 1, 28, 29):
                if sequence < 30:
                    loop_edges[sequence] = frame.planes
                else:
                    assert frame.planes == loop_edges[sequence % 30]
            player.mark_presented(frame)
        assert player.media_pos_ns() == frame.pts
        assert player.decoder_restarts == 0
    finally:
        started = time.monotonic()
        player.stop()
        assert time.monotonic() - started < 3


def test_new_clip_does_not_reuse_old_frames_or_timing(video):
    from player import Player
    player = Player(decoder="avdec_h264")
    try:
        for _ in range(2):
            player.prepare(str(video))
            frame = player.take_frame(0)
            assert frame is not None and frame.pts == 0
    finally:
        player.stop()


def test_nonlooping_input_drains_tail_frames(video):
    from player import Player
    player = Player(decoder="avdec_h264", buffer_frames=3)
    try:
        player.prepare(str(video), loop=False)
        deadline = time.monotonic() + 5
        for sequence in range(30):
            frame = None
            while frame is None and time.monotonic() < deadline:
                frame = player.take_frame(sequence)
                if frame is None:
                    time.sleep(.001)
            assert frame is not None, f"missing tail frame {sequence}"
        assert player.error is None
    finally:
        player.stop()


@pytest.mark.parametrize('stride', [18, 32])
def test_decoded_nv12_packing_preserves_pixels_and_owns_unmapped_data(stride):
    from player import Player, Gst, GstVideo
    width, height = 18, 8
    chroma_offset = stride * height + 64
    raw = bytearray([251]) * (chroma_offset + stride * height // 2)
    expected = []
    for plane, offset, rows in ((0, 0, height), (1, chroma_offset, height // 2)):
        pixels = bytes((i + plane * 91) % 240 for i in range(width * rows))
        expected.append(pixels)
        for row in range(rows):
            raw[offset + row * stride:offset + row * stride + width] = pixels[row * width:(row + 1) * width]
    buffer = Gst.Buffer.new_allocate(None, len(raw), None)
    assert buffer.fill(0, raw) == len(raw)
    buffer.pts = 0
    GstVideo.buffer_add_video_meta_full(buffer, GstVideo.VideoFrameFlags.NONE,
        GstVideo.VideoFormat.NV12, width, height, 2,
        [0, chroma_offset, 0, 0], [stride, stride, 0, 0])
    segment = Gst.Segment()
    segment.init(Gst.Format.TIME)
    sample = Gst.Sample.new(buffer, Gst.Caps.from_string(
        f'video/x-raw,format=NV12,width={width},height={height},framerate=30/1'), segment, None)

    class Sink:
        def emit(self, name):
            assert name == 'pull-sample'
            return sample

    player = Player(decoder='avdec_h264', buffer_frames=3)
    try:
        assert player._on_sample(Sink()) == Gst.FlowReturn.OK, player.error
        frame = player.take_frame(0)
        # The callback has already unmapped the input. Frames must retain packed,
        # independently owned bytes, with neither stride padding nor plane gaps.
        assert all(isinstance(plane, bytes) for plane in frame.planes)
        assert frame.planes == tuple(expected)
        del sample, buffer
        assert frame.planes == tuple(expected)
    finally:
        player.stop()


def test_compressible_video_does_not_demux_many_loops_before_presentation(tmp_path):
    from player import Player
    path = tmp_path / 'compressible.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=black:size=320x240:rate=30:duration=10',
                    '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    assert path.stat().st_size < 100_000

    class Trace:
        def __init__(self):
            self.loops = []

        def record(self, event, **fields):
            if event == 'encoded_loop':
                self.loops.append(fields['loop'])

    player = Player(decoder='avdec_h264', buffer_frames=3)
    trace = player.trace = Trace()
    try:
        player.prepare(str(path))
        time.sleep(.2)
        assert player.buffered_range() == (0, 2)
        assert trace.loops == []
        assert player._encoded.source.get_property('current-level-buffers') <= 33
        assert player._encoded.source.get_property('current-level-time') <= 1_100_000_000
        assert player.error is None
    finally:
        player.stop()
