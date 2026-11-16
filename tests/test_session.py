"""Real 1080p decoders and GstNet clocks through the master frame protocol."""
import asyncio
import json
import hashlib
import subprocess
import shutil
import time

from test_player import video


def test_cancelling_frame_shutdown_preserves_caller_cancellation():
    from controller import Controller
    import pytest

    async def run():
        cleanup_started = asyncio.Event()

        async def frame_loop():
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await asyncio.Event().wait()

        master = Controller.__new__(Controller)
        master._frame_task = asyncio.create_task(frame_loop())
        await asyncio.sleep(0)
        shutdown = asyncio.create_task(master._cancel_frame_task())
        await asyncio.wait_for(cleanup_started.wait(), 1)
        shutdown.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(shutdown, 1)

    asyncio.run(run())


class DecoderNode:
    def __init__(self, name, master, path, clock_port):
        from player import Player
        self.name, self.master, self.path, self.clock_port = name, master, path, clock_port
        self.player = Player(decoder="avdec_h264")
        self.session = None
        self.messages = asyncio.Queue()
        self.commits = asyncio.Queue()
        self.presented = []
        self.at = []
        self.staged = {}

    async def send(self, data):
        await self.messages.put(json.loads(data))

    async def report(self, data):
        await self.master._on_node_msg(self.name, json.dumps(data))

    async def receive(self):
        while True:
            message = await self.messages.get()
            if message["cmd"] == "prepare":
                self.session = message["session"]
                spec = await asyncio.to_thread(self.player.prepare, str(self.path))
                await asyncio.to_thread(self.player.slave_to_clock, "127.0.0.1", self.clock_port)
                await self.report({"type": "ready", "session": self.session, "spec": spec})
            elif message["cmd"] == "stage_frame":
                frame = self.player.take_frame(message["sequence"])
                assert frame is not None
                self.staged[frame.sequence] = frame
                await self.report({"type": "frame_staged", "session": self.session, "sequence": frame.sequence})
            elif message["cmd"] == "commit_frame":
                await self.commits.put(message)
            elif message["cmd"] == "stop":
                self.session = None
                await asyncio.to_thread(self.player.stop)

    async def status(self):
        while True:
            if self.session and self.player.spec:
                first, last = self.player.buffered_range()
                await self.report({"type": "frame_status", "session": self.session,
                    "buffered_first": first, "buffered_last": last,
                    "presented": self.presented[-1] if self.presented else -1})
            await asyncio.sleep(.005)

    async def present(self):
        while True:
            message = await self.commits.get()
            while self.player.clock_time_ns() < message["present_at_ns"]:
                await asyncio.sleep(.001)
            frame = self.staged.pop(message["sequence"])
            assert frame is not None
            self.player.mark_presented(frame)
            self.presented.append(frame.sequence)
            actual = self.player.clock_time_ns()
            self.at.append(actual)
            await self.report({"type": "frame_presented", "session": self.session,
                "sequence": frame.sequence, "frame": message["frame"],
                "presented_at_ns": actual,
                "presentation_error_ns": actual - message["present_at_ns"]})


@__import__("pytest").mark.parametrize("tiled", [False, True])
def test_master_commits_same_frames_across_three_full_hd_decoders_and_loops(video, tmp_path, tiled):
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstNet", "1.0")
    from gi.repository import Gst, GstNet
    from controller import Controller
    from roommodel import RoomModel

    async def run():
        media = tmp_path / "media"
        media.mkdir()
        shutil.copy(video, media / "test.mp4")
        room = RoomModel(tmp_path / "room.json")
        for i in range(3):
            room.ensure_node(f"node-{i}")
        master = Controller(room, 0, 0, 0, media_dir=media)
        provider = GstNet.NetTimeProvider.new(Gst.SystemClock.obtain(), "127.0.0.1", 0)
        master.clock_port = provider.get_property("port")
        paths = {name: video for name in room.nodes()}
        media_name = "test.mp4"
        if tiled:
            package = {"format": "immersive-tiled-v1", "fps_num": 30, "fps_den": 1,
                       "frames": 30, "nodes": {}}
            for i, name in enumerate(room.nodes()):
                path = media/f"tile-{i}.mp4"
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video),
                    "-vf", f"hue=h={i*70}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
                package["nodes"][name] = {"file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "width": 1920, "height": 1080,
                    "region": {"x": i/3, "y": 0, "w": 1/3, "h": 1}}
                room.update_field(name, 'source_region', package['nodes'][name]['region'])
                paths[name] = path
            assert len({a["sha256"] for a in package["nodes"].values()}) == 3
            media_name = "show.immersive.json"
            (media/media_name).write_text(json.dumps(package))
        nodes = [DecoderNode(name, master, paths[name], master.clock_port) for name in room.nodes()]
        tasks = []
        for node in nodes:
            master.nodes[node.name] = node
            master.node_info[node.name] = {"protocol": 2, "capabilities": ["per_node_video_v1"]}
            tasks.extend(asyncio.create_task(coro) for coro in (node.receive(), node.status(), node.present()))
        try:
            await asyncio.wait_for(master.cmd_play_media(media_name), 15)
            deadline = time.monotonic() + 8
            while min(len(node.presented) for node in nodes) < 65:
                assert master.show.get("error") is None
                for task in tasks:
                    if task.done():
                        task.result()
                assert time.monotonic() < deadline, master.show
                await asyncio.sleep(.01)
            for node in nodes:
                assert node.presented[:65] == list(range(65))
                assert node.player.error is None
            # Software presentation is checked independently of frame identity.
            # This does not assert physical projector scanout/genlock.
            skew = max(max(n.at[i] for n in nodes) - min(n.at[i] for n in nodes) for i in range(65))
            assert skew < 33_333_334
            assert skew <= master.show["max_presentation_skew_ns"] < 33_333_334
            # A main-thread status snapshot may arrive after a newer flip
            # acknowledgement from the independent presentation worker.
            node = nodes[0]
            before = master._coordinator.presented[node.name]
            first, last = node.player.buffered_range()
            await master._on_node_msg(node.name, json.dumps({"type": "frame_status",
                "session": master._session, "buffered_first": first,
                "buffered_last": last, "presented": before-1}))
            assert master._coordinator.presented[node.name] >= before
            assert master.show.get("error") is None
            # A disconnected session member stops the fleet, rather than leaving
            # the other projectors silently running a different frame sequence.
            await master._fail_session("node-1 disconnected")
            assert master.show["playing"] is False
            assert master.show["state"] == "error"
        finally:
            await master._cancel_frame_task()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for node in nodes:
                await asyncio.to_thread(node.player.stop)
            provider = None
    asyncio.run(run())
