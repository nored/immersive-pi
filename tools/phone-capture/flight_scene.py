"""Looping observer flight through a stationary, layered asteroid field.

The observer translates on one axis. Longitudinal wrap happens only for distant,
faded objects, preserving a continuous loop without rolling the room horizon.
"""
import math
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/"test-media"))
import numpy as np
import cv2
from PIL import Image,ImageDraw
from make_native_meteor import Scene


class FlightScene(Scene):
    def __init__(self,*args,forward_fraction=.125,**kwargs):
        super().__init__(*args,**kwargs)
        self.course_length=110.
        heading=2*math.pi*forward_fraction-math.pi
        self.forward=np.array([math.cos(heading),math.sin(heading),0.])
        self.side=np.array([-math.sin(heading),math.cos(heading),0.])
        rng=np.random.default_rng(4917)
        self.field=[]
        for i in range(72):
            size=rng.uniform(.3,1.0)
            lateral=(1 if rng.random()<.5 else -1)*(1.5*size+rng.uniform(.6,16 if i%3 else 5))
            self.field.append((lateral,rng.uniform(0,self.course_length),rng.uniform(-.7,.7),size,
                               int(rng.integers(0,3)),rng.uniform(0,2*math.pi)))

    def centers(self,phase):
        centers=[];alphas=[]
        for lateral,longitudinal,z,size,turns,seed in self.field:
            along=(longitudinal-phase*self.course_length+self.course_length/2)%self.course_length-self.course_length/2
            centers.append(self.side*lateral+self.forward*along+[0,0,z])
            alphas.append(min(1,max(0,(self.course_length/2-abs(along))/9)))
        return np.asarray(centers),np.asarray(alphas)

    def render_background(self,phase):
        return self.background.copy()

    def render(self,frame):
        phase=(frame%self.frames)/self.frames;scale=self.width/(2*math.pi)
        centers,alphas=self.centers(phase);polygons=[]
        for rock,center,alpha in zip(self.field,centers,alphas):
            _,_,_,size,turns,seed=rock
            if alpha<.003:continue
            angle=turns*2*math.pi*phase+seed;c,s=math.cos(angle),math.sin(angle)
            points=self.vertices@np.array([[c,-s,0],[s,c,0],[0,0,1]]).T*size+center
            center_azimuth=math.atan2(center[1],center[0])
            azimuth=np.arctan2(points[:,1],points[:,0])
            azimuth=center_azimuth+(azimuth-center_azimuth+math.pi)%(2*math.pi)-math.pi
            projected=np.column_stack(((azimuth+math.pi)*scale,
                self.height/2-points[:,2]/np.maximum(np.linalg.norm(points[:,:2],axis=1),1e-6)*scale))
            if projected[:,1].max()<0 or projected[:,1].min()>=self.height:continue
            triangles=points[self.faces];normals=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
            normals/=np.maximum(np.linalg.norm(normals,axis=1,keepdims=True),1e-9)
            face_centers=triangles.mean(1);visible=np.einsum('ij,ij->i',normals,face_centers)<0
            shades=(.22+.78*np.maximum(0,normals@self.light))*self.tint
            colors=np.minimum(255,shades[:,None]*getattr(self,'rock_color',[159,146,128])+3).astype('u1')
            depths=np.linalg.norm(face_centers,axis=1)
            for i in np.flatnonzero(visible):polygons.append((depths[i],projected[self.faces[i]],tuple(map(int,colors[i]))+(round(alpha*255),)))
        image=self.render_background(phase);draw=ImageDraw.Draw(image,'RGBA')
        for _,polygon,color in sorted(polygons,key=lambda p:p[0],reverse=True):
            for offset in (-self.width,0,self.width):
                if polygon[:,0].max()+offset>=0 and polygon[:,0].min()+offset<self.width:
                    draw.polygon([(float(x+offset),float(y)) for x,y in polygon],fill=color)
        return image


class FallScene(FlightScene):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.course_length=60.
        rng=np.random.default_rng(4918)
        self.field=[]
        for _ in range(144):
            size=rng.uniform(.3,1.2)
            radius=1.8*size+rng.uniform(1.0,25)
            self.field.append((radius,rng.uniform(0,2*math.pi),rng.uniform(-30,30),size,
                               int(rng.integers(0,3)),rng.uniform(0,2*math.pi)))
        self.stars=[]
        for _ in range(3000):
            self.stars.append((rng.uniform(45,130),rng.uniform(0,2*math.pi),rng.uniform(-30,30),
                               rng.uniform(60,175),int(rng.integers(3,10)),rng.uniform(0,2*math.pi)))

    def centers(self,phase):
        centers=[];alphas=[]
        for radius,heading,z,size,turns,seed in self.field:
            vertical=(z+phase*self.course_length+30)%self.course_length-30
            centers.append([radius*math.cos(heading),radius*math.sin(heading),vertical])
            alphas.append(min(1,max(0,(30-abs(vertical))/5)))
        return np.asarray(centers),np.asarray(alphas)

    def star_positions(self,phase):
        stars=np.asarray(self.stars);radius,heading,z,brightness,turns,seed=stars.T
        vertical=(z+phase*self.course_length+30)%self.course_length-30
        scale=self.width/(2*math.pi)
        x=((heading+math.pi)%(2*math.pi))*scale
        y=self.height/2-vertical/radius*scale
        twinkle=.85+.15*np.sin(2*math.pi*phase*turns+seed)
        fade=np.clip((30-abs(vertical))/3,0,1)
        return np.column_stack([x,y,brightness*twinkle*fade])

    def render_background(self,phase):
        image=Image.new('RGB',(self.width,self.height),(2,4,10))
        draw=ImageDraw.Draw(image)
        for x,y,brightness in self.star_positions(phase):
            if not 0<=y<self.height or brightness<2:continue
            b=int(brightness);color=(b,b,min(255,b+25))
            draw.point((round(x)%self.width,round(y)),fill=color)
            if b>125:
                draw.line((round(x)%self.width,round(y),round(x)%self.width,round(y+2)),fill=(b//2,b//2,min(255,b//2+20)))
        return image


class PlungeScene(FallScene):
    """Dense enclosing rock chasm with accelerating observer descent."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.course_length=24.
        self.rock_color=[215,195,165]
        rng=np.random.default_rng(4919)
        self.field=[]
        for _ in range(320):
            size=rng.uniform(.25,1.3)
            radius=1.5*size+rng.uniform(.6,12)
            self.field.append((radius,rng.uniform(0,2*math.pi),rng.uniform(-12,12),size,
                               int(rng.integers(0,3)),rng.uniform(0,2*math.pi)))
        self.stars=[(rng.uniform(45,130),rng.uniform(0,2*math.pi),rng.uniform(-12,12),
                     rng.uniform(90,210),int(rng.integers(3,10)),rng.uniform(0,2*math.pi)) for _ in range(3400)]
        tw,th=min(8192,max(512,self.width)),min(1024,max(128,self.height*2))
        texture=np.zeros((th,tw),dtype='f4')
        for w,h,amplitude in [(128,16,65),(512,64,35),(2048,256,15),(8192,1024,8)]:
            noise=rng.random((min(h,th),min(w,tw))).astype('f4')
            texture+=cv2.resize(noise,(tw,th),interpolation=cv2.INTER_CUBIC)*amplitude
        y,x=np.mgrid[:th,:tw].astype('f4')
        strata=np.sin(2*math.pi*y/170+.7*np.sin(2*math.pi*x/tw*11))
        texture=np.clip(texture+22*strata,12,190)
        rgb=np.clip(texture[:,:,None]*np.array([1.0,.88,.72]),0,255).astype('u1')
        atlas=Image.fromarray(rgb);draw=ImageDraw.Draw(atlas)
        for _ in range(max(4,round(180*tw*th/(8192*1024)))):
            sx=int(rng.integers(0,tw));sy=int(rng.integers(-th,th));points=[(sx,sy)]
            for step in range(1,7):points.append((sx+int(rng.integers(-45,46)),sy+step*int(rng.integers(25,55))))
            for offset in (-th,0,th):
                line=[(px,(py+offset)) for px,py in points]
                draw.line(line,fill=(13,11,10),width=4)
                draw.line([(px+5,py) for px,py in line],fill=(175,148,111),width=2)
        self.atlas=np.asarray(atlas)
        self.texture_period_m=6.
        yy,xx=np.mgrid[:self.height,:self.width].astype('f4')
        theta=xx/self.width*2*math.pi
        radius=6.5+1.3*np.sin(theta*5)+.6*np.sin(theta*11)
        self.wall_x=(xx/self.width*tw).astype('f4')
        self.wall_y=((self.height/2-yy)/(self.width/(2*math.pi))*radius/self.texture_period_m*th).astype('f4')
        self.shade=(.78+.22*np.cos(theta*5+.6))[:,:,None].astype('f4')

    @staticmethod
    def progress(phase):
        # Positive speed, slow entry and pronounced acceleration/deceleration;
        # two complete course lengths guarantee a continuous loop.
        return 2*phase-1.8/(2*math.pi)*math.sin(2*math.pi*phase)

    def centers(self,phase):
        travel=self.progress(phase)*self.course_length
        centers=[];alphas=[]
        for radius,heading,z,size,turns,seed in self.field:
            vertical=(z+travel+12)%24-12
            centers.append([radius*math.cos(heading),radius*math.sin(heading),vertical])
            alphas.append(min(1,max(0,(12-abs(vertical))/2)))
        return np.asarray(centers),np.asarray(alphas)

    def star_positions(self,phase):
        stars=np.asarray(self.stars);radius,heading,z,brightness,turns,seed=stars.T
        vertical=(z+self.progress(phase)*24+12)%24-12
        scale=self.width/(2*math.pi)
        return np.column_stack([((heading+math.pi)%(2*math.pi))*scale,
            self.height/2-vertical/radius*scale,
            brightness*(.85+.15*np.sin(2*math.pi*phase*turns+seed))*np.clip((12-abs(vertical))/2,0,1)])

    def render_background(self,phase):
        th=self.atlas.shape[0]
        offset=self.progress(phase)*24/self.texture_period_m*th
        # Sampling downward in the atlas makes the whole surrounding wall rise.
        y=((self.wall_y-offset)%th).astype('f4')
        sampled=cv2.remap(self.atlas,self.wall_x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP)
        image=Image.fromarray(np.clip(sampled.astype('f4')*self.shade,0,255).astype('u1'))
        draw=ImageDraw.Draw(image)
        for x,y,brightness in self.star_positions(phase):
            if not 0<=y<self.height or brightness<10:continue
            b=int(brightness);tail=max(3,min(24,round(2+18*(1-math.cos(2*math.pi*phase))/2)))
            draw.line((round(x)%self.width,round(y),round(x)%self.width,round(y+tail)),fill=(b//2,b//2,min(255,b//2+25)))
            draw.point((round(x)%self.width,round(y)),fill=(b,b,min(255,b+25)))
        return image
