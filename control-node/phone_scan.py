"""Phone-owned scan sessions on the existing WebSocket connection."""
import asyncio
import json
import os
from pathlib import Path
import time
import uuid

from shared.phone_scan import Coverage, PROTOCOL, make_targets


class PhoneScan:
    def __init__(self,controller):
        self.controller=controller
        self.state=None
        self.lock=asyncio.Lock()
        self.phase_timeout=8

    @property
    def active(self):return self.state is not None

    def catalog(self):
        ctl=self.controller
        result=[]
        for node in ctl.room.nodes():
            hb=ctl.heartbeats.get(node,{})
            graphics=hb.get('graphics',{})
            result.append({'node':node,'online':node in ctl.nodes,
                           'capture_supported':'phone_scan_v1' in ctl.node_info.get(node,{}).get('capabilities',[]),
                           'display_ready':bool(hb.get('fb_ok')) and not hb.get('display_error'),
                           'width':graphics.get('output_width'),'height':graphics.get('output_height')})
        return {'type':'capture_catalog','protocol':PROTOCOL,'nodes':result,
                'busy':self.active,'calibration_validated':False}

    def _log(self,state,event,**fields):
        event={'event':event,'master_monotonic_ns':time.monotonic_ns(),**fields}
        with state['journal'].open('a') as stream:stream.write(json.dumps(event,allow_nan=False)+'\n')

    async def process(self,owner,message):
        cmd=message.get('cmd')
        request_id=message.get('request_id')
        if owner is None:raise ValueError('phone scan requires an owning WebSocket')
        if cmd=='capture_catalog':
            await self.controller._send(owner,{**self.catalog(),'request_id':request_id});return
        async with self.lock:
            try:
                if cmd=='capture_begin':
                    if self.active:raise ValueError('another phone scan already owns the room')
                    capture_id=str(uuid.UUID(message['capture_id']))
                    nodes=self.catalog()['nodes']
                    if not nodes or any(not all(n[k] for k in ('online','capture_supported','display_ready')) for n in nodes):
                        raise ValueError('all configured projectors must be online with phone scan support and a ready display')
                    if self.controller.power_state!='awake':raise ValueError('wake the room before scanning')
                    devices=getattr(self.controller,'devices',None)
                    if devices and devices.base:
                        await devices.refresh()
                        blockers=devices.blockers()
                        if blockers:raise ValueError('projectors are not confirmed ready: '+', '.join(blockers))
                    room_version=self.controller.room.model['version']
                    session=uuid.uuid4().hex
                    try:layout=await asyncio.to_thread(make_targets,self.controller.media_dir,session,nodes)
                    except Exception as exc:raise ValueError('could not create native scan targets: '+str(exc)) from exc
                    if self.controller.room.model['version']!=room_version:
                        raise ValueError('room changed while targets were being prepared')
                    await self.controller.cmd_stop()
                    directory=Path(os.environ.get('IMMERSIVE_STATE_DIR',self.controller.media_dir/'scan-state'))/'phone-scans'
                    directory.mkdir(parents=True,exist_ok=True)
                    state={'owner':owner,'session':session,'capture_id':capture_id,'layout':layout,
                           'coverage':Coverage(layout),'phase':0,'context':None,'event':asyncio.Event(),
                           'acks':{},'error':None,'last_seen':time.monotonic(),'journal':directory/(session+'.jsonl')}
                    self.state=state
                    self._log(state,'begin',capture_id=capture_id,room_version=room_version,layout=layout)
                    state['watchdog']=asyncio.create_task(self._watchdog(state))
                    context=await self._phase(state,'overview',None)
                    await self.controller._send(owner,{'type':'capture_started','request_id':request_id,
                        'protocol':PROTOCOL,'session':session,'capture_id':capture_id,'layout':layout,
                        'context':context,'coverage':state['coverage'].snapshot()})
                    return
                state=self.state
                if state is None or state['owner'] is not owner or message.get('session')!=state['session']:
                    raise ValueError('no matching phone-owned scan session')
                state['last_seen']=time.monotonic()
                if cmd=='capture_phase':
                    context=await self._phase(state,message.get('kind'),message.get('node'))
                    reply={'type':'capture_phase','context':context}
                elif cmd=='capture_observation':
                    context=state['context']
                    if not context or not context['ready'] or message.get('phase')!=context['phase']:
                        raise ValueError('observation belongs to a stale or unacknowledged display phase')
                    node=state['coverage'].observe(message,phase_kind=context['kind'],target=context['node'])
                    self._log(state,'observation',packet=message)
                    reply={'type':'capture_coverage','coverage':state['coverage'].snapshot(),'detected_node':node}
                elif cmd=='capture_ping':reply={'type':'capture_ping','context':state['context']}
                elif cmd=='capture_end':
                    coverage=state['coverage'].snapshot()
                    await self._end(state,'operator finished',notify=False)
                    reply={'type':'capture_ended','coverage':coverage}
                else:raise ValueError('unknown phone scan command')
                await self.controller._send(owner,{**reply,'request_id':request_id,'session':state['session']})
            except (ValueError,KeyError,TypeError,OSError,asyncio.TimeoutError) as exc:
                # A rejected observation adds no evidence; control failures end
                # the session so a camera cannot continue under a false phase.
                if cmd not in ('capture_observation','capture_ping') and self.state and self.state['owner'] is owner:
                    await self._end(self.state,str(exc),notify=True)
                raise ValueError(str(exc)) from exc

    async def _phase(self,state,kind,node):
        if kind not in ('overview','fiducials','white','black'):raise ValueError('unknown scan pattern kind')
        if kind!='overview' and node not in state['coverage'].cells:raise ValueError('select a configured projector')
        if set(self.controller.room.nodes())-self.controller.nodes.keys():raise ValueError('a projector disconnected')
        state['phase']+=1
        state['acks']={};state['event'].clear();state['error']=None
        context={'session':state['session'],'phase':state['phase'],'kind':kind,'node':node,
                 'layout_id':state['layout']['layout_id'],'ready':False}
        state['context']=context
        self._log(state,'phase_request',context=context)
        expected={}
        for target in state['layout']['nodes']:
            pattern='fiducials' if kind=='overview' else kind if target['node']==node else 'black'
            command={'cmd':'capture_pattern','session':state['session'],'phase':state['phase'],
                     'kind':pattern,'width':target['width'],'height':target['height']}
            if pattern=='fiducials':command.update(file=target['file'],sha256=target['sha256'])
            expected[target['node']]=command
        state['expected']=expected
        for name,command in expected.items():
            if state['error']:raise ValueError(state['error'])
            await self.controller._send_node(name,command)
        await asyncio.wait_for(state['event'].wait(),timeout=self.phase_timeout)
        if state['error']:raise ValueError(state['error'])
        if set(state['acks'])!=set(expected):raise ValueError('scan display acknowledgements are incomplete')
        context={**context,'ready':True,'display_acks':dict(state['acks']),
                 'master_ready_monotonic_ns':time.monotonic_ns(),
                 'settle_after_receipt_s':.75,'optical_display_verified':False}
        state['context']=context
        self._log(state,'phase_ready',context=context)
        return context

    def receipt(self,node,message):
        state=self.state
        if not state or message.get('session')!=state['session'] or message.get('phase')!=state['phase']:return
        expected=state.get('expected',{}).get(node)
        if not expected:return
        if message.get('type')=='capture_error':
            state['error']=f'{node}: {message.get("error","scan display failed")}'
        elif any(message.get(k)!=expected.get(k) for k in ('kind','width','height','sha256')):
            state['error']=f'{node}: scan display acknowledgement does not match the requested asset'
        elif type(message.get('flip_monotonic_ns')) is not int or message['flip_monotonic_ns']<=0:
            state['error']=f'{node}: scan display acknowledgement has no DRM flip timestamp'
        else:state['acks'][node]=message
        if state['error'] or set(state['acks'])==set(state.get('expected',{})):state['event'].set()
        if state['error'] and state['context'].get('ready'):
            state['context']={**state['context'],'ready':False}
            asyncio.create_task(self.cancel(state['error']))

    def display_status(self,node,display):
        state=self.state
        if not state or not state.get('context',{}).get('ready'):return
        expected=state.get('expected',{}).get(node)
        if expected and (not isinstance(display,dict) or any(display.get(k)!=expected.get(k)
                for k in ('session','phase','kind','width','height','sha256'))):
            state['context']={**state['context'],'ready':False}
            asyncio.create_task(self.cancel(f'{node}: scan pattern no longer active'))

    async def _end(self,state,reason,*,notify):
        if self.state is not state:return
        self.state=None
        watchdog=state.get('watchdog')
        if watchdog and watchdog is not asyncio.current_task():watchdog.cancel()
        for node in state['coverage'].cells:
            await self.controller._send_node(node,{'cmd':'capture_pattern','kind':'clear','session':state['session']})
        try:self._log(state,'end',reason=reason,coverage=state['coverage'].snapshot())
        except OSError:pass
        if notify:
            try:await self.controller._send(state['owner'],{'type':'capture_aborted','session':state['session'],'reason':reason})
            except Exception:pass

    async def cancel(self,reason,owner=None):
        state=self.state
        if not state or (owner is not None and state['owner'] is not owner):return
        state['error']=reason;state['event'].set()
        async with self.lock:
            await self._end(state,reason,notify=True)

    async def _watchdog(self,state):
        while self.state is state:
            await asyncio.sleep(2)
            if time.monotonic()-state['last_seen']>15:
                await self.cancel('phone scan heartbeat expired');return
