"""Exercise HTTP Basic login, same-origin WebSocket, and node status in browsers."""
import asyncio
import base64
import functools
import io
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

playwright = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize('browser_name', ['chromium', 'firefox'])
def test_source_editor_rejects_invalid_input_keeps_socket_and_exports_only_accepted_edits(browser_name, site):
    import json
    from pathlib import Path
    async def baseline():
        site.ctl.room.ensure_node('node-2')
        site.ctl.room.save('baseline', commit=False)
    asyncio.run_coroutine_threadsafe(baseline(), site.loop).result(5)
    before = site.ctl.room.path.read_bytes()
    with playwright.sync_playwright() as runner:
        browser = getattr(runner, browser_name).launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(site.url)
            page.wait_for_selector('#conn.up')
            page.evaluate('window.originalSocket = state.ws')
            page.evaluate("send({cmd:'set_source_region',node:'node-1',source_region:{x:0,y:0,w:0,h:1}})")
            page.wait_for_function("document.querySelector('#source-status').textContent.includes('positive dimensions')")
            assert page.evaluate('state.ws === window.originalSocket && state.ws.readyState === 1')
            assert page.evaluate("entryFor('node-1').source_region.w") == 1
            page.locator('#source-w').fill('75')
            page.locator('#source-apply').click()
            page.wait_for_function("entryFor('node-1').source_region.w === .75")
            assert '270.00°' in page.locator('#source-angle').inner_text()
            assert 'Applied' in page.locator('#source-status').inner_text()
            assert site.ctl.room.entry_for('node-2')['source_region']['w'] == 1
            with page.expect_download() as download:
                page.locator('#download-room-model').click()
            exported = json.loads(Path(download.value.path()).read_text())
            assert exported['nodes'][0]['source_region']['w'] == .75
            assert exported['nodes'][1]['source_region']['w'] == 1
            assert site.ctl.room.path.read_bytes() == before
        finally:
            browser.close()


@pytest.mark.parametrize('browser_name', ['chromium', 'firefox'])
def test_room_source_grid_stops_playback_then_addresses_every_enrolled_node(browser_name, site):
    commands=[]
    async def configure():
        site.ctl.room.ensure_node('node-2')
        original_stop=site.ctl.cmd_stop
        async def stop():
            commands.append(('stop',))
            await original_stop()
        async def send(node,message):commands.append((node,message))
        site.ctl.cmd_stop=stop
        site.ctl._send_node=send
    asyncio.run_coroutine_threadsafe(configure(), site.loop).result(5)
    with playwright.sync_playwright() as runner:
        browser=getattr(runner,browser_name).launch(headless=True)
        page=browser.new_page()
        try:
            page.goto(site.url)
            page.wait_for_selector('#conn.up')
            page.locator('#room-source-grid').click()
            page.wait_for_timeout(100)
            assert commands[0]==('stop',)
            assert [(node,m['kind']) for node,m in commands[1:]] == [
                ('node-1','source-grid'),('node-2','source-grid')]
            assert 'active' in page.locator('[data-pat="source-grid"]').get_attribute('class')
            page.locator('[data-step="overlap"]').click()
            assert page.evaluate('state.pattern')=='source-grid'
        finally:
            browser.close()


@pytest.fixture
def site(tmp_path, monkeypatch):
    import websockets
    import controller
    import nodeadmin
    from roommodel import RoomModel
    config = tmp_path / "immersive.conf"
    config.write_text("node=test-control\nadmin_password=browser-test-password\n")
    monkeypatch.setattr(nodeadmin, "BOOT_CONF_PATHS", [str(config)])
    ready = threading.Event()
    state = SimpleNamespace()
    async def serve():
        room = RoomModel(tmp_path / "room.json")
        room.ensure_node("node-1")
        media = tmp_path / "media"
        media.mkdir()
        (media / "test.mp4").touch()
        ctl = controller.Controller(room, 0, 0, 0, media_dir=media)
        ctl._loop = asyncio.get_running_loop()
        state.loop, state.ctl = ctl._loop, ctl
        state.stop = asyncio.Event()
        async with websockets.serve(ctl.handler, "127.0.0.1", 0) as websocket:
            ctl.ws_port = websocket.sockets[0].getsockname()[1]
            controller._ApiHandler.controller = ctl
            handler = functools.partial(controller._ApiHandler, directory=str(controller.WEB_DIR))
            http = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            state.url = f"http://127.0.0.1:{http.server_port}"
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            ready.set()
            try:
                await state.stop.wait()
            finally:
                await ctl._cancel_frame_task()
                await asyncio.to_thread(http.shutdown)
                http.server_close()
    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    assert ready.wait(10)
    yield state
    state.loop.call_soon_threadsafe(state.stop.set)
    thread.join(10)
    assert not thread.is_alive()


@pytest.mark.parametrize("browser_name", ["chromium", "firefox"])
def test_login_controls_and_status(browser_name, site):
    from PIL import Image
    with playwright.sync_playwright() as runner:
        browser = getattr(runner, browser_name).launch(headless=True)
        context = browser.new_context(http_credentials={"username": "admin", "password": "browser-test-password"})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(site.url + "/admin")
            page.wait_for_selector("#cfg input")
            assert page.url.endswith("/admin/")
            assert page.locator("#status").inner_text() == ""
            page.locator("#f_node").fill("unsaved-edit")
            page.wait_for_timeout(5200)
            assert page.locator("#f_node").input_value() == "unsaved-edit"
            page.goto(site.url)
            page.wait_for_selector("#conn.up")
            page.wait_for_selector("#dot-node-1")
            assert page.locator("#thumbs img").count() == 0
            assert page.locator("#backdrop").count() == 0
            assert page.locator("#mesh-canvas").is_visible()
            page.locator("[data-show=play]").click()
            page.wait_for_function("document.getElementById('playback-status').textContent.includes('disconnected')")
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("browser_name", ["chromium", "firefox"])
def test_local_camera_preview_starts_only_on_request_and_stops(browser_name, site):
    with playwright.sync_playwright() as runner:
        options = {'headless': True}
        if browser_name == 'chromium':
            options['args'] = ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream']
        else:
            options['firefox_user_prefs'] = {'media.navigator.streams.fake': True,
                                            'media.navigator.permission.disabled': True}
        browser = getattr(runner, browser_name).launch(**options)
        try:
            page = browser.new_page()
            page.goto(site.url+'/camera.html')
            assert page.evaluate('document.getElementById("preview").srcObject === null')
            page.locator('#start').click()
            page.wait_for_function('!document.getElementById("capture").disabled')
            assert page.evaluate('document.getElementById("preview").videoWidth > 0')
            assert page.evaluate('document.getElementById("preview").srcObject.getAudioTracks().length') == 0
            page.evaluate('window.testTracks = document.getElementById("preview").srcObject.getTracks()')
            page.locator('#stop').click()
            assert page.evaluate('window.testTracks.every(t => t.readyState === "ended")')
            assert page.evaluate('document.getElementById("preview").srcObject === null')
        finally:
            browser.close()


@pytest.mark.parametrize('browser_name', ['chromium', 'firefox'])
def test_room_device_actual_status_and_command(browser_name, site):
    calls = []
    async def request(path, method='GET'):
        calls.append((path, method))
        return {'beamer': [{'nr': 4, 'name': 'Rechts vorne', 'ist': 'aus',
                            'ziel': 'an', 'antwortet': True, 'fehler': ''}]}
    async def configure():
        site.ctl.devices.base = 'http://fake-node-red'
        site.ctl.devices._request = request
        await site.ctl.devices.refresh()
    asyncio.run_coroutine_threadsafe(configure(), site.loop).result(5)
    with playwright.sync_playwright() as runner:
        browser = getattr(runner, browser_name).launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(site.url)
            page.locator('#room-devices summary').click()
            page.wait_for_function("document.querySelector('#projector-list').textContent.includes('4 · Rechts vorne: aus · target an')")
            page.locator('[data-device="panorama"][data-action="on"]').click()
            page.wait_for_timeout(150)
            assert ('/projector/dortmund/panorama/on', 'POST') in calls
        finally:
            browser.close()


def test_http_stop_cancels_waiting_start_with_json_response(site):
    import concurrent.futures
    import json
    import urllib.error
    import urllib.request
    entered = threading.Event()
    async def configure():
        site.ctl.devices.base = 'http://fake-device-api'
        async def ready(notify):
            entered.set()
            await asyncio.Event().wait()
        site.ctl.devices.ready = ready
    asyncio.run_coroutine_threadsafe(configure(), site.loop).result(5)
    def request(path, body=b'{}'):
        req = urllib.request.Request(site.url + path, data=body,
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            return response.code, json.load(response)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        start = executor.submit(request, '/api/play', b'{"media":"test.mp4"}')
        assert entered.wait(2)
        assert request('/api/stop') == (200, {'playing': False})
        assert start.result(3) == (409, {'error': 'Room operation cancelled'})
    assert site.ctl._start_task is None
    assert not site.ctl.show['playing']


@pytest.mark.parametrize('browser_name', ['chromium', 'firefox'])
def test_idle_stale_and_disconnected_nodes_have_distinct_status(browser_name, site):
    with playwright.sync_playwright() as runner:
        browser = getattr(runner, browser_name).launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(site.url)
            page.wait_for_selector('#conn.up')
            async def publish():
                await site.ctl._broadcast_web({'type': 'nodes', 'nodes_up': ['node-1']})
                await site.ctl._broadcast_web({'type': 'heartbeat', 'hb': {
                    'node': 'node-1', 'decoder_ok': False, 'fb_ok': True}})
            asyncio.run_coroutine_threadsafe(publish(), site.loop).result(5)
            page.wait_for_selector('#dot-node-1.idle')
            assert 'waiting for video' in page.locator('#dot-node-1').get_attribute('title')
            page.evaluate("state.heartbeatSeen['node-1'] = performance.now() - 5000; renderThumbDots()")
            assert 'bad' in page.locator('#dot-node-1').get_attribute('class')
            assert 'no recent heartbeat' in page.locator('#dot-node-1').get_attribute('title')
            page.evaluate('state.nodesUp = []; renderThumbDots()')
            assert page.locator('#dot-node-1').get_attribute('class').strip() == 'dot'
            assert page.locator('#dot-node-1').get_attribute('title') == 'Disconnected from master'
        finally:
            browser.close()
