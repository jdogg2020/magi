"""Durable, station-wide admission and publication for opt-in video recovery."""
from collections import deque
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import struct
import threading
import time
import uuid

from . import event_tracks as contract
from .event_lifetime import LOCK, POSTPROCESSING, pin
from .event_review import read as read_review
from .recovery_service import send, receive

TEMPORARY_BYTES=256*1024*1024


def bounded_json(path, default=None):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('not a regular file')
            return contract._load(contract._read(fd))
        finally: os.close(fd)
    except FileNotFoundError:
        if default is not None: return default
        raise


class EventHandle:
    def __init__(self,cfg,directory):
        self.cfg,self.directory=cfg,Path(directory)
        if self.directory.parent!=cfg.root/'events': raise ValueError('event outside recording root')
        self.fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        self.info=os.fstat(self.fd)
        st=cfg.mount.stat()
        self.volume=(st.st_dev,st.st_ino,(cfg.mount/'.magi-volume').read_text())

    def close(self): os.close(self.fd)

    def check(self):
        st=self.cfg.mount.stat()
        if (st.st_dev,st.st_ino,(self.cfg.mount/'.magi-volume').read_text())!=self.volume:
            raise OSError('recording volume changed')
        if self.cfg.require_mount and not self.cfg.mount.is_mount(): raise OSError('volume unavailable')
        current=self.directory.lstat()
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev,current.st_ino)!=(self.info.st_dev,self.info.st_ino):
            raise OSError('event replaced or deleted')
        if read_review(self.directory).get('trashed_at') is not None: raise InterruptedError('event trashed')

    def write(self,name,value):
        self.check()
        temporary='.recovery-'+uuid.uuid4().hex+'.tmp'
        raw=contract.canonical_bytes(value)
        if len(raw)>contract.MAX_BYTES: raise ValueError('recovery product too large')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.fd)
        try:
            with os.fdopen(fd,'wb') as out:
                out.write(raw);out.flush();os.fsync(out.fileno())
            self.check()
            os.rename(temporary,name,src_dir_fd=self.fd,dst_dir_fd=self.fd);os.fsync(self.fd)
        finally:
            try: os.unlink(temporary,dir_fd=self.fd)
            except FileNotFoundError: pass

    @contextmanager
    def lock(self,name):
        fd=os.open(name,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self.fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise ValueError('unsafe lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield
        finally: os.close(fd)


def publish(handle,event,camera_id,payload,request_id):
    timing=event if event['detection_settings']['stream']=='mainstream' else bounded_json(handle.directory/'substream.json')
    contract.validate_revision(payload,event,camera_id,timing)
    source_name=contract.SOURCE_FILES[event['detection_settings']['stream']]
    info=(handle.directory/source_name).lstat()
    if not stat.S_ISREG(info.st_mode) or [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]!=payload['work']['source_identity']:
        raise ValueError('source changed before publication')
    payload={**payload,'request_id':request_id}
    revision=contract.digest(payload)
    with LOCK,handle.lock('.tracks-publication.lock'):
        handle.check()
        handle.write(f'tracks-{revision}.json',payload)
        handle.write('tracks.json',{'schema_version':1,'event_id':event['id'],'camera_id':camera_id,
                     'revision':revision,'file':f'tracks-{revision}.json','sha256':revision,
                     'published_unix':time.time(),'geometry_sha256':payload['geometry_sha256'],'request_id':request_id})
        # Keep current and current derivative references. Open reader/export
        # descriptors survive unlink; no unbounded archive history on the Pi.
        derivative=bounded_json(handle.directory/'media.json',{})
        keep={revision,derivative.get('track_revision')}
        for name in os.listdir(handle.fd):
            if name.startswith('tracks-') and name.endswith('.json') and contract._hash(name[7:-5]) and name[7:-5] not in keep:
                os.unlink(name,dir_fd=handle.fd)
    return revision


class Client:
    def __init__(self,cfg,camera_id,media):
        self.cfg,self.camera_id,self.media=cfg,camera_id,media
        self.stopped=threading.Event()
        self.cursor=None
        self.started_unix=time.time()
        self.scan_lock=threading.RLock()
        if cfg.recovery_enabled: SCHEDULER.register(self)

    def close(self):
        self.stopped.set();SCHEDULER.unregister(self)
        with self.scan_lock:
            if self.cursor is not None: self.cursor.close();self.cursor=None

    def scan(self):
        with self.scan_lock:
            if self.stopped.is_set(): return []
            return self._scan()

    def _scan(self):
        # Iterator survives passes: at most 100 entries per pass, no list of the archive.
        if self.cursor is None: self.cursor=os.scandir(self.cfg.root/'events')
        result=[]
        for _ in range(100):
            try: entry=next(self.cursor)
            except StopIteration:
                self.cursor.close();self.cursor=None;break
            if not entry.is_dir(follow_symlinks=False): continue
            directory=Path(entry.path)
            try:
                event=bounded_json(directory/'event.json')
                if event.get('state') not in ('complete','interrupted') or not event.get('track_samples'): continue
                if read_review(directory).get('trashed_at') is not None: continue
                state=bounded_json(directory/'recovery.json',{})
                # Existing archive events require an explicit reprocess request.
                if not state and event.get('trigger_unix',0)<self.started_unix: continue
                stream=event.get('detection_settings',{}).get('stream')
                if stream == 'substream':
                    timing=bounded_json(directory/'substream.json',{})
                    if timing.get('state') not in ('complete','interrupted'):
                        if state.get('state')!='waiting_media':
                            with LOCK:
                                handle=EventHandle(self.cfg,directory)
                                try:
                                    with handle.lock('.recovery-job.lock'):
                                        handle.write('recovery.json',{'schema_version':1,'state':'waiting_media','requested_unix':time.time()})
                                finally:handle.close()
                        continue
                evidence=contract.evidence_hash(event,self.camera_id)
                if state.get('evidence_sha256')==evidence and state.get('state') in ('complete','partial','unavailable','failed','cancelled'):
                    if state.get('active_revision') and state.get('state') in ('complete','partial'):
                        media=bounded_json(directory/'media.json',{})
                        if media.get('track_revision')!=state['active_revision']:
                            self.media.submit(directory,event,force=True)
                    continue
                if state.get('state') not in ('queued','running') or state.get('evidence_sha256')!=evidence:
                    with LOCK:
                        handle=EventHandle(self.cfg,directory)
                        try:
                            with handle.lock('.recovery-job.lock'):
                                state={'schema_version':1,'state':'queued','generation':uuid.uuid4().hex,
                                       'evidence_sha256':evidence,'requested_unix':time.time()}
                                handle.write('recovery.json',state)
                        finally:handle.close()
                result.append((state.get('requested_unix',event.get('trigger_unix',0)),directory))
            except (OSError,ValueError,TypeError,KeyError): continue
        return sorted(result,key=lambda row:(row[0],row[1].name))

    def request(self,directory):
        if not self.cfg.recovery_enabled: raise ValueError('Enable experimental track recovery before reprocessing')
        with LOCK:
            handle=EventHandle(self.cfg,directory)
            try:
                with handle.lock('.recovery-job.lock'):
                    event=bounded_json(Path(directory)/'event.json')
                    state={'schema_version':1,'state':'queued','generation':uuid.uuid4().hex,'requested_unix':time.time(),
                           'evidence_sha256':contract.evidence_hash(event,self.camera_id)}
                    handle.write('recovery.json',state)
            finally: handle.close()
        SCHEDULER.wake.set()

    def process(self,directory):
        handle=EventHandle(self.cfg,directory)
        try:
            with pin(directory,handle.check),handle.lock('.recovery-job.lock'):
                # Remove only our abandoned atomic-write temporaries while holding the job lock.
                for name in os.listdir(handle.fd):
                    if name.startswith('.recovery-') and name.endswith('.tmp') and len(name)==46:
                        os.unlink(name,dir_fd=handle.fd)
                event=bounded_json(directory/'event.json')
                evidence=contract.evidence_hash(event,self.camera_id)
                state=bounded_json(directory/'recovery.json',{})
                manifest=bounded_json(directory/'tracks.json',{})
                generation=state.get('generation') or uuid.uuid4().hex
                if manifest.get('request_id')==generation:
                    previous=contract.read_tracks(directory,event,camera_id=self.camera_id,mode='extended')
                    if previous['status'] in ('complete','partial'):
                        handle.write('recovery.json',{**state,'state':previous['status'],'active_revision':previous['revision']});return
                state={**state,'schema_version':1,'state':'running','evidence_sha256':evidence,
                       'generation':generation,'attempt':state.get('attempt',0)+1,'started_unix':time.time(),
                       'requested_unix':state.get('requested_unix',time.time()),'active_revision':manifest.get('revision')}
                with LOCK: handle.write('recovery.json',state)
                try:
                    import shutil
                    if shutil.disk_usage(self.cfg.root).free-self.cfg.min_free<TEMPORARY_BYTES:
                        raise ValueError('temporary_space_unavailable')
                    payload=self.invoke(handle,state)
                    if self.stopped.is_set(): raise InterruptedError('recovery cancelled')
                    current=bounded_json(directory/'event.json')
                    if contract.evidence_hash(current,self.camera_id)!=evidence: raise ValueError('capture_evidence_changed')
                    if not payload.get('tracks'):
                        state.update(state='unavailable',reason='no_supported_recovery',work=payload.get('work',{}),finished_unix=time.time())
                        with LOCK: handle.write('recovery.json',state)
                        return
                    revision=publish(handle,current,self.camera_id,payload,generation)
                    state.update(state=payload['status'],active_revision=revision,work=payload['work'],finished_unix=time.time())
                    with LOCK: handle.write('recovery.json',state)
                    # Regeneration uses the same bounded media queue and explicit source.
                    self.media.submit(directory,current,force=True)
                except Exception as error:
                    state.update(state='cancelled' if self.stopped.is_set() else 'failed',reason=str(error)[:240],finished_unix=time.time())
                    with LOCK: handle.write('recovery.json',state)
        finally: handle.close()

    def invoke(self,handle,state):
        connection=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        connection.settimeout(12)
        pid=None; complete=False
        try:
            connection.connect('/run/magi-recovery.sock')
            pid,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            # Socket activation reports PID 1 as the listener peer on some
            # kernels; never signal that PID. Systemd owns worker cleanup.
            with connection.makefile('rwb') as stream:
                hello=receive(stream,4096)
                candidate=hello.get('pid')
                if type(candidate) is int and candidate>1 and Path(f'/proc/{candidate}').stat().st_uid==os.getuid():
                    pid=candidate
                else: pid=None
                if not hello.get('limited'): raise ValueError('resource_limits_unavailable')
                active=0.
                with POSTPROCESSING.acquire('recovery',self.stopped.is_set):
                    started=time.monotonic()
                    send(stream,{'directory':str(handle.directory),'camera_id':self.camera_id})
                    reply=receive(stream)
                    active+=time.monotonic()-started
                while reply.get('type')=='checkpoint':
                    handle.check()
                    if self.stopped.is_set() or active>60: raise InterruptedError('recovery cancelled or deadline reached')
                    with LOCK: handle.write('recovery.json',{**state,'progress':reply['work']})
                    with POSTPROCESSING.acquire('recovery',self.stopped.is_set):
                        connection.settimeout(3.5)
                        started=time.monotonic()
                        send(stream,{'action':'continue'});reply=receive(stream)
                        active+=time.monotonic()-started
                if reply.get('type')!='result': raise ValueError(reply.get('reason','invalid worker response'))
                complete=True
                return reply['payload']
        finally:
            connection.close()
            if not complete and pid is not None and pid>1:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass
            # systemd KillMode=control-group reaps remaining decoder children.


class Scheduler:
    def __init__(self):
        self.lock=threading.Lock();self.clients=[];self.thread=None;self.wake=threading.Event()

    def register(self,client):
        with self.lock:
            self.clients.append(client)
            if self.thread is None or not self.thread.is_alive():
                self.thread=threading.Thread(target=self.run,daemon=True,name='magi-recovery-supervisor');self.thread.start()
        self.wake.set()

    def unregister(self,client):
        with self.lock:
            if client in self.clients:self.clients.remove(client)
        self.wake.set()

    def run(self):
        ready=deque()
        while True:
            with self.lock: clients=list(self.clients)
            if not clients: return
            ready=deque((c,p) for c,p in ready if c in clients and not c.stopped.is_set())
            # Persist pending requests on disk; retain at most four ready paths
            # station-wide. Rotate cameras after each admission pass.
            for client in clients:
                if client.stopped.is_set() or len(ready)>=4: continue
                try:
                    known={(id(c),str(p)) for c,p in ready}
                    for _,directory in client.scan():
                        if (id(client),str(directory)) not in known:
                            ready.append((client,directory))
                            break
                except (OSError,ValueError,TypeError,KeyError): pass
            with self.lock:
                if self.clients:self.clients.append(self.clients.pop(0))
            if ready:
                client,directory=ready.popleft()
                try:
                    if not client.stopped.is_set():client.process(directory)
                except (OSError,ValueError,TypeError,KeyError): pass
            self.wake.wait(1);self.wake.clear()


SCHEDULER=Scheduler()
