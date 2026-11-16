#!/usr/bin/env python3
"""Vendor only the reviewed runtime files into Yocto; never room data or media."""
import argparse
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from runtime_release import build


def stage(repository, destination):
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=Path(tempfile.mkdtemp(prefix='immersive-image-app-',dir=destination.parent))
    try:
        archive=temporary/'runtime.tar.gz'
        metadata=build(repository,archive)
        output=temporary/'app';output.mkdir()
        with tarfile.open(archive) as source:
            for entry in source.getmembers():
                if not entry.name.startswith('app/') or not entry.isfile():continue
                relative=Path(entry.name[len('app/'):])
                if relative.is_absolute() or '..' in relative.parts:raise ValueError('Unsafe app path')
                path=output/relative;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(source.extractfile(entry).read())
            (output/'runtime-manifest.json').write_bytes(source.extractfile('manifest.json').read())
        # The destination is generated build input, never a live application.
        if destination.exists():shutil.rmtree(destination)
        output.replace(destination)
        return {'release':metadata['release'],'destination':str(destination)}
    finally:
        shutil.rmtree(temporary)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(stage(Path(__file__).resolve().parents[1],args.out)))
