"""A cubemap must recover spherical directions before room sampling."""
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/phone-capture'))
from video_scene import RealVideoScene


@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='Requires ffmpeg v360')
def test_eac_decode_recovers_direction_colours_in_wall_band(tmp_path):
    width, height = 768, 384
    longitude = (np.arange(width) + .5) * (2*np.pi/width) - np.pi
    latitude = np.pi/2 - (np.arange(height) + .5) * (np.pi/height)
    x = np.cos(latitude)[:, None] * np.sin(longitude)[None, :]
    y = np.broadcast_to(np.sin(latitude)[:, None], x.shape)
    z = np.cos(latitude)[:, None] * np.cos(longitude)[None, :]
    original = np.rint((np.stack([x,y,z], axis=2)+1)*127.5).astype('uint8')
    image = tmp_path/'directions.png';Image.fromarray(original).save(image)
    source = tmp_path/'cubemap.mkv'
    subprocess.run(['ffmpeg','-v','error','-loop','1','-framerate','1','-i',str(image),
                    '-vf',f'v360=input=equirect:output=eac:w={width}:h={height}',
                    '-frames:v','2','-c:v','ffv1','-pix_fmt','bgr0',str(source)], check=True)
    scene = RealVideoScene(source, 1, Fraction(1), projection='eac')
    try:actual = scene.render(0).copy()
    finally:scene.close()
    expected = original[height//4:height*3//4]
    error = np.abs(actual.astype(float)-expected.astype(float))
    assert error.mean() < 2
    assert np.percentile(error,99) < 5
