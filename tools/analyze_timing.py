#!/usr/bin/env python3
"""Summarize opt-in master/render JSONL traces from one renderer trial."""
from pathlib import Path
import argparse
import json,statistics
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("master", type=Path)
parser.add_argument("render", type=Path)
args = parser.parse_args()
m=[json.loads(l) for l in args.master.open()];r=[json.loads(l) for l in args.render.open()]
start=max(i for i,x in enumerate(m) if x['event']=='reserve' and x['sequence']==0);m=m[start:]
start=max(i for i,x in enumerate(r) if x['event']=='decoded' and x['sequence']==0);r=r[start:]
commits={x['sequence']:x for x in m if x['event']=='commit'};res={x['sequence']:x for x in m if x['event']=='reserve'}
lookup={(x['event'],x.get('cmd'),x.get('sequence')):x for x in r}
stats={}
for i,c in commits.items():
 stage=lookup.get(('stage_done',None,i));recv=lookup.get(('command_received','stage_frame',i));apply=lookup.get(('command_apply','stage_frame',i))
 if not all((stage,recv,apply)):continue
 values={'roundtrip':(c['local_ns']-res[i]['local_ns'])/1e6,'node_work':(stage['local_ns']-recv['local_ns'])/1e6,'queue':(apply['local_ns']-recv['local_ns'])/1e6,'draw':stage['draw_ms'],'native':stage['native_ms']}
 values['outside_node']=values['roundtrip']-values['node_work']
 for k,v in values.items():stats.setdefault(k,[]).append(v)
 if c['held']:print('HOLD',i,{k:round(v,2) for k,v in values.items()},stage['buffered'])
print('frames',len(commits),'holds',sum(c['held'] for c in commits.values()))
for key,vals in stats.items():print(key,'median',round(statistics.median(vals),2),'p99',round(sorted(vals)[int(len(vals)*.99)],2),'max',round(max(vals),2))
v=[x['copy_ms'] for x in r if x['event']=='decoded'];print('decode copy ms median',statistics.median(v),'max',max(v))
