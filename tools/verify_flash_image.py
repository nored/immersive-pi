#!/usr/bin/env python3
"""Read-only inspection of the actual MBR flash image, including its boot FAT."""
import argparse
import json
from pathlib import Path
import struct
import subprocess
import tempfile

from verify_image import Ext4, verify


def layout(path):
    with path.open('rb') as stream:
        mbr=stream.read(512)
    assert mbr[510:512]==b'\x55\xaa', 'Missing MBR signature'
    parts=[]
    for i in range(4):
        entry=struct.unpack('<BBBBBBBBII',mbr[446+i*16:462+i*16])
        start,size=entry[8]*512,entry[9]*512
        assert start>0 and size>0 and start+size<=path.stat().st_size, 'Invalid partition bounds'
        parts.append({'number':i+1,'type':entry[4],'offset':start,'size':size})
    for a,b in zip(parts,parts[1:]):
        assert a['offset']+a['size']<=b['offset'], 'Overlapping partitions'
    assert parts[0]['type'] in (0xb,0xc) and all(p['type']==0x83 for p in parts[1:]), 'Unexpected partition types'
    return parts,f'{int.from_bytes(mbr[440:444],"little"):08x}'


def extract(image,partition,destination):
    with image.open('rb') as source,destination.open('wb') as output:
        source.seek(partition['offset'])
        left=partition['size']
        while left:
            data=source.read(min(left,4*1024*1024))
            if not data:raise ValueError('Truncated flash image')
            output.write(data);left-=len(data)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',type=Path,required=True)
    parser.add_argument('--release',required=True)
    parser.add_argument('--version',required=True)
    parser.add_argument('--public-key',type=Path)
    args=parser.parse_args()
    parts,disk_id=layout(args.image)
    with tempfile.TemporaryDirectory(prefix='immersive-flash-inspect-') as temporary:
        root=Path(temporary)/'root.ext4';boot=Path(temporary)/'boot.fat'
        extract(args.image,parts[0],boot);extract(args.image,parts[1],root)
        files={name:subprocess.run(['mtype','-i',str(boot),'::/'+name],capture_output=True,check=True).stdout
               for name in ('config.txt','cmdline.txt','immersive.conf')}
        result=verify(Ext4(root),files,args.release,args.version,
                      args.public_key.read_bytes() if args.public_key else None)
        selector=[token for token in files['cmdline.txt'].decode().split() if token.startswith('root=')][-1]
        assert selector in ('root=/dev/mmcblk0p2','root=PARTUUID='+disk_id+'-02'), 'Boot selects the wrong root slot'
        result.update(partitions=parts,disk_id=disk_id,boot_root=selector,actual_flash_image_verified=True)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
