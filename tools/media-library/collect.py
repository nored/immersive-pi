#!/usr/bin/env python3
"""Fetch a curated source catalog and index actual 360° source files.

No login, paid-media downloader, or unbounded site crawler. Provenance and reuse
terms travel with every source. Native room conversion is a separate stage.
"""
import argparse,base64,hashlib,json,shutil,subprocess,zipfile
from pathlib import Path


def digest(path,algorithm='sha256'):
    value=hashlib.new(algorithm)
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):value.update(block)
    return value


def plain(root,name):
    if not isinstance(name,str) or Path(name).name!=name or name in ('','.','..'):raise ValueError('Invalid local source filename')
    return root/name


def collect(catalog,output,fetch=False):
    output.mkdir(parents=True,exist_ok=True);rows=[]
    for entry in catalog['sources']:
        directory=(output/entry.get('directory','.')).resolve()
        if not directory.is_relative_to(output.resolve()):raise ValueError('Source directory escapes library')
        directory.mkdir(parents=True,exist_ok=True)
        target=plain(directory,entry['file'])
        if not target.exists() and fetch:
            download=plain(directory,entry.get('archive_file',entry['file']))
            if not download.exists():
                partial=download.with_name(download.name+'.part')
                subprocess.run(['curl','-fL','-A','Mozilla/5.0','--retry','2','--connect-timeout','15','-C','-',
                                '--output',str(partial),entry['download_url']],check=True)
                if entry.get('expected_download_bytes') and partial.stat().st_size!=entry['expected_download_bytes']:raise ValueError('Incomplete download')
                if entry.get('download_md5_base64') and base64.b64encode(digest(partial,'md5').digest()).decode()!=entry['download_md5_base64']:raise ValueError('Publisher download checksum mismatch')
                partial.rename(download)
            if entry.get('archive_member'):
                with zipfile.ZipFile(download) as archive:
                    with archive.open(entry['archive_member']) as source,target.open('xb') as destination:shutil.copyfileobj(source,destination,4*1024*1024)
        row={**entry,'local_file':str(target),'ready_for_room':False}
        if target.exists():
            if entry.get('expected_sha1') and digest(target,'sha1').hexdigest()!=entry['expected_sha1']:raise ValueError('Publisher source checksum mismatch')
            row['sha256']=digest(target).hexdigest();row['bytes']=target.stat().st_size
            metadata=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_streams','-show_format','-of','json',str(target)]))
            stream=metadata['streams'][0];row.update(width=stream['width'],height=stream['height'],duration_s=float(stream.get('duration') or metadata['format']['duration']))
            row['format_compatible']=abs(row['width']/row['height']-2)<.01
            row['status']='source_downloaded' if row['format_compatible'] else 'projection_review_required'
            row['spherical_metadata']=stream.get('side_data_list',[])
        else:row['status']='not_downloaded'
        rows.append(row)
    result={'sources':rows,'note':'Sources are not yet selectable native room packages unless separately rendered and uploaded.'}
    (output/'index.json').write_text(json.dumps(result,indent=2)+'\n')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('catalog',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--fetch',action='store_true');a=p.parse_args()
    result=collect(json.loads(a.catalog.read_text()),a.output,a.fetch)
    print(json.dumps([{'title':r['title'],'status':r['status'],'resolution':[r.get('width'),r.get('height')]} for r in result['sources']]))
