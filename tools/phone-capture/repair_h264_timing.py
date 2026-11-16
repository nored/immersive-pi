#!/usr/bin/env python3
"""Add absent H.264 VUI timing while copying compressed picture data unchanged.

VideoToolbox streams can omit SPS timing entirely; GStreamer then negotiates
framerate=0/1 despite valid MP4 timestamps. This remux preserves image NAL units
and packet timestamps, adding only minimal SPS VUI timing for the declared CFR.
"""
import argparse,hashlib,json
from fractions import Fraction
from pathlib import Path
import av


class Bits:
    def __init__(self,data):self.bits=''.join(f'{b:08b}' for b in data);self.pos=0
    def read(self,n=1):
        if self.pos+n>len(self.bits):raise ValueError('Truncated SPS')
        value=int(self.bits[self.pos:self.pos+n],2);self.pos+=n;return value
    def ue(self):
        zeros=0
        while self.read()==0:
            zeros+=1
            if zeros>32:raise ValueError('Invalid SPS exponential code')
        return (1<<zeros)-1+(self.read(zeros) if zeros else 0)
    def se(self):
        code=self.ue();return (code+1)//2 if code%2 else -code//2


def add_sps_timing(nal,fps):
    if nal[0]&31!=7:return nal,False
    rbsp=nal[1:].replace(b'\0\0\3',b'\0\0');r=Bits(rbsp)
    profile=r.read(8);r.read(8);r.read(8);r.ue()
    if profile in (100,110,122,244,44,83,86,118,128,138,139,134,135):
        chroma=r.ue()
        if chroma==3:r.read()
        r.ue();r.ue();r.read()
        if r.read():
            for i in range(8 if chroma!=3 else 12):
                if r.read():
                    last=next_scale=8
                    for _ in range(16 if i<6 else 64):
                        if next_scale:next_scale=(last+r.se()+256)%256
                        if next_scale:last=next_scale
    r.ue();order=r.ue()
    if order==0:r.ue()
    elif order==1:
        r.read();r.se();r.se()
        for _ in range(r.ue()):r.se()
    r.ue();r.read();r.ue();r.ue()
    if not r.read():r.read()
    r.read()
    if r.read():
        for _ in range(4):r.ue()
    offset=r.pos
    if r.read():return nal,False  # Preserve existing VUI verbatim.
    if not 0<fps.denominator<2**32 or not 0<2*fps.numerator<2**32:raise ValueError('Timing does not fit H.264 VUI')
    vui='00001'+f'{fps.denominator:032b}'+f'{2*fps.numerator:032b}'+'10000'
    result=r.bits[:offset]+'1'+vui+'1';result+='0'*((-len(result))%8)
    raw=bytes(int(result[i:i+8],2) for i in range(0,len(result),8));escaped=bytearray();zeros=0
    for b in raw:
        if zeros>=2 and b<=3:escaped.append(3);zeros=0
        escaped.append(b);zeros=zeros+1 if b==0 else 0
    return nal[:1]+escaped,True


def patch_avcc(extra,fps):
    if len(extra)<7 or extra[0]!=1:raise ValueError('An AVC configuration record is required')
    result=bytearray(extra[:6]);offset=6;changes=0
    for _ in range(extra[5]&31):
        size=int.from_bytes(extra[offset:offset+2],'big');offset+=2
        nal=extra[offset:offset+size]
        if len(nal)!=size:raise ValueError('Truncated AVC SPS')
        replacement,changed=add_sps_timing(nal,fps);changes+=changed
        result.extend(len(replacement).to_bytes(2,'big'));result.extend(replacement);offset+=size
    result.extend(extra[offset:])
    return bytes(result),changes


def packet_nals(data,length_size):
    offset=0
    while offset<len(data):
        if offset+length_size>len(data):raise ValueError('Truncated AVC packet length')
        size=int.from_bytes(data[offset:offset+length_size],'big');offset+=length_size
        nal=data[offset:offset+size]
        if not nal or len(nal)!=size:raise ValueError('Invalid AVC NAL size')
        yield nal;offset+=size


def repair(source,destination,fps):
    if destination.exists():raise ValueError('Timing repair output already exists')
    pictures=hashlib.sha256();count=0;inband=0
    with av.open(str(source)) as original,av.open(str(destination),'w',options={'movflags':'+faststart'}) as output:
        stream=original.streams.video[0]
        if stream.average_rate!=fps:raise ValueError('Declared repair rate differs from the MP4 timing')
        length_size=(stream.codec_context.extradata[4]&3)+1
        extradata,changes=patch_avcc(stream.codec_context.extradata,fps)
        target=output.add_stream_from_template(stream);target.codec_context.extradata=extradata;target.time_base=stream.time_base
        for packet in original.demux(stream):
            if not packet.size:continue
            replacement=bytearray();changed=False
            for nal in packet_nals(bytes(packet),length_size):
                kind=nal[0]&31
                if kind in (1,5):pictures.update(len(nal).to_bytes(4,'big'));pictures.update(nal)
                fixed,did_change=add_sps_timing(nal,fps);changed|=did_change;inband+=did_change
                replacement.extend(len(fixed).to_bytes(length_size,'big'));replacement.extend(fixed)
            if changed:
                updated=av.Packet(bytes(replacement));updated.pts=packet.pts;updated.dts=packet.dts
                updated.duration=packet.duration;updated.time_base=packet.time_base;updated.is_keyframe=packet.is_keyframe;packet=updated
            packet.stream=target;output.mux(packet);count+=1
    verify=hashlib.sha256();verified=0
    with av.open(str(destination)) as result:
        stream=result.streams.video[0];length_size=(stream.codec_context.extradata[4]&3)+1
        for packet in result.demux(stream):
            if not packet.size:continue
            for nal in packet_nals(bytes(packet),length_size):
                if nal[0]&31 in (1,5):verify.update(len(nal).to_bytes(4,'big'));verify.update(nal)
            verified+=1
    if pictures.digest()!=verify.digest() or count!=verified:raise ValueError('Compressed picture data changed during remux')
    return {'file':destination.name,'packets':count,'sps_headers_updated':changes+inband,
            'compressed_picture_sha256':pictures.hexdigest(),'compressed_picture_data_unchanged':True,
            'fps_num':fps.numerator,'fps_den':fps.denominator}


def ensure_timing(path,fps):
    """Repair a hardware output in place only after picture integrity passes."""
    with av.open(str(path)) as container:
        extra=container.streams.video[0].codec_context.extradata
        _,changes=patch_avcc(extra,fps)
    if not changes:return {'file':path.name,'already_has_vui':True,'compressed_picture_data_unchanged':True}
    temporary=path.with_name(path.stem+'.timing-fixed.mp4')
    report=repair(path,temporary,fps)
    temporary.replace(path)
    report['file']=path.name
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('source',type=Path);p.add_argument('destination',type=Path);p.add_argument('--fps',type=Fraction,required=True);a=p.parse_args();print(json.dumps(repair(a.source,a.destination,a.fps)))
