"""Local-only protocol simulator. Never contacts projectors or the production backend."""
import asyncio
import json
import sys
import websockets

async def main():
    async def protocol(ws):
        phase = 0
        async for raw in ws:
            msg = json.loads(raw)
            cmd = msg.get('cmd')
            if cmd == 'capture_catalog':
                reply = dict(type='capture_catalog', protocol=1, nodes=[{'node':'p1'}])
            elif cmd == 'capture_begin':
                reply = dict(type='capture_started',session='test-session',layout={'nodes':[{'node':'p1','markers':[{'payload':'target1'}]}]},context={'phase':phase,'kind':'overview','ready':True})
            elif cmd == 'capture_phase':
                phase += 1
                reply = dict(type='capture_phase',context={'phase':phase,'kind':msg['kind'],'ready':True})
            elif cmd in ('capture_end','capture_ping'):
                reply = dict(type=cmd)
            else:
                continue
            reply['request_id'] = msg['request_id']
            await ws.send(json.dumps(reply))
    async def handle(ws):
        try:
            await protocol(ws)
        except websockets.exceptions.ConnectionClosed:
            pass
    async with websockets.serve(handle,'127.0.0.1',0) as server:
        port = server.sockets[0].getsockname()[1]
        process = await asyncio.create_subprocess_exec(sys.argv[1],f'127.0.0.1:{port}')
        result = await process.wait()
        if result:
            raise SystemExit(result)

asyncio.run(main())
