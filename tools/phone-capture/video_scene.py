"""Sequential full-resolution equirectangular footage decoder for mapped clips."""
import json,subprocess
from fractions import Fraction
import numpy as np


class RealVideoScene:
    def __init__(self,path,frames,fps,*,start_seconds=0,hardware=False,projection='equirect'):
        self.path=path;self.frames=frames;self.next=0
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=width,height,codec_name:format=duration','-of','json',str(path)]))
        stream=probe['streams'][0];self.width=stream['width'];self.original_height=stream['height']
        if projection not in ('equirect','eac'):raise ValueError('Unsupported source projection')
        if abs(self.width/self.original_height-2)>.01:raise ValueError('Real footage must have the supported 2:1 source dimensions')
        if start_seconds<0 or start_seconds+frames/float(fps)>float(probe['format']['duration']):raise ValueError('Requested clip exceeds the source duration')
        self.crop_top=self.original_height//4;self.height=self.original_height//2
        # Resample time before expensive cubemap conversion. Render only the
        # central 90-degree wall band, at the same angular pixel density.
        filters=[f'fps={fps}']
        if projection=='eac':
            filters.append(f'v360=input=eac:output=equirect:w={self.width}:h={self.height}:v_fov=90')
        else:filters.append(f'crop={self.width}:{self.height}:0:{self.crop_top}')
        self.hardware_decode=hardware and stream.get('codec_name') in ('h264','hevc','vp9')
        self.process=subprocess.Popen(['ffmpeg','-nostdin','-v','error']+(['-hwaccel','videotoolbox'] if self.hardware_decode else [])+['-ss',str(start_seconds),'-i',str(path),
            '-vf',','.join(filters),
            '-frames:v',str(frames),'-an','-pix_fmt','rgb24','-f','rawvideo','pipe:1'],stdout=subprocess.PIPE,stderr=subprocess.PIPE)

    def render(self,frame):
        if frame!=self.next:raise ValueError('Source frames must be consumed sequentially')
        size=self.width*self.height*3;data=self.process.stdout.read(size)
        if len(data)!=size:raise ValueError('Video source ended before all requested frames decoded')
        self.next+=1
        return np.frombuffer(data,dtype='u1').reshape(self.height,self.width,3)

    def close(self):
        if self.next<self.frames and self.process.poll() is None:self.process.kill()
        self.process.stdout.close()
        try:code=self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill();self.process.wait();raise
        error=self.process.stderr.read().decode()
        if self.next==self.frames and code:raise RuntimeError(error)
