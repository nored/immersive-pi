import asyncio
import http.client
from http.server import ThreadingHTTPServer
import json
import os
import socket
import tempfile
import threading
from types import SimpleNamespace
import urllib.request

import pytest

from controller import _ApiHandler


@pytest.fixture
def media_site(tmp_path):
    loop=asyncio.new_event_loop()
    loop_thread=threading.Thread(target=loop.run_forever,daemon=True);loop_thread.start()
    messages=[]
    async def broadcast(message):messages.append(message)
    controller=SimpleNamespace(media_dir=tmp_path,_loop=loop,_broadcast_web=broadcast,
                               playlist=lambda:{'videos':sorted(p.name for p in tmp_path.glob('*.mp4'))})
    hook=[None]
    class Handler(_ApiHandler):
        def log_message(self,*args):pass
        def end_headers(self):
            if self.command=='GET' and self.path.startswith('/media/') and hook[0]:
                callback,hook[0]=hook[0],None;callback()
            super().end_headers()
    Handler.controller=controller
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    yield SimpleNamespace(root=tmp_path,port=server.server_port,hook=hook,messages=messages)
    server.shutdown();server.server_close();worker.join(2)
    loop.call_soon_threadsafe(loop.stop);loop_thread.join(2);loop.close()


def upload_socket(site,length,body):
    connection=socket.create_connection(('127.0.0.1',site.port),timeout=3)
    header=f'POST /api/upload?name=movie.mp4 HTTP/1.0\r\nContent-Length: {length}\r\n\r\n'
    connection.sendall(header.encode()+body)
    return connection


def response(connection):
    result=http.client.HTTPResponse(connection);result.begin()
    return result.status,json.loads(result.read())


def download(site):
    with urllib.request.urlopen(f'http://127.0.0.1:{site.port}/media/movie.mp4',timeout=3) as result:
        return result.read()


def test_interrupted_upload_preserves_previous_video(media_site):
    target=media_site.root/'movie.mp4';target.write_bytes(b'complete previous video')
    with upload_socket(media_site,100,b'partial') as connection:
        connection.shutdown(socket.SHUT_WR)
        status,body=response(connection)
    assert status==400 and 'declared bytes' in body['error']
    assert target.read_bytes()==b'complete previous video'
    assert not list(media_site.root.glob('.upload-*'))
    assert not media_site.messages


def test_in_progress_upload_keeps_old_video_readable_until_complete(media_site,monkeypatch):
    target=media_site.root/'movie.mp4';target.write_bytes(b'old video')
    created=threading.Event();original=tempfile.mkstemp
    def mkstemp(*args,**kwargs):
        result=original(*args,**kwargs);created.set();return result
    monkeypatch.setattr(tempfile,'mkstemp',mkstemp)
    new=b'new complete video'*40
    with target.open('rb') as old_reader,upload_socket(media_site,len(new),new[:20]) as connection:
        assert created.wait(2)
        assert download(media_site)==b'old video'
        connection.sendall(new[20:]);connection.shutdown(socket.SHUT_WR)
        assert response(connection)[0]==200
        assert download(media_site)==new
        assert old_reader.read()==b'old video'
    assert not list(media_site.root.glob('.upload-*'))


def test_download_headers_and_body_use_the_same_open_file(media_site):
    target=media_site.root/'movie.mp4';target.write_bytes(b'old video')
    replacement=media_site.root/'replacement';replacement.write_bytes(b'new content of a different length')
    media_site.hook[0]=lambda:os.replace(replacement,target)
    assert download(media_site)==b'old video'
    assert download(media_site)==b'new content of a different length'


@pytest.mark.parametrize('length',['0','-1','not-a-number'])
def test_invalid_upload_length_does_not_touch_existing_video(media_site,length):
    target=media_site.root/'movie.mp4';target.write_bytes(b'old video')
    with upload_socket(media_site,length,b'') as connection:
        assert response(connection)[0]==400
    assert target.read_bytes()==b'old video'
