"""Socket-activated recovery process; isolated from the recorder by systemd."""
import json
import os
from pathlib import Path
import socket
import struct


def limits_available():
    try:
        relative = next(line.split(':',2)[2] for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        root = Path('/sys/fs/cgroup')/relative.lstrip('/')
        quota, period = (root/'cpu.max').read_text().split()
        memory = (root/'memory.max').read_text().strip()
        return quota != 'max' and int(quota)/int(period) <= .5 and memory != 'max' and int(memory) <= 384*1024*1024
    except (OSError,ValueError,StopIteration):
        return False


def send(stream,value):
    stream.write(json.dumps(value,separators=(',',':'),allow_nan=False).encode()+b'\n');stream.flush()


def receive(stream,limit=4*1024*1024):
    raw=stream.readline(limit+1)
    if not raw or len(raw)>limit or not raw.endswith(b'\n'):
        raise ValueError('invalid worker message')
    value=json.loads(raw)
    if not isinstance(value,dict): raise ValueError('invalid worker message')
    return value


def handle(connection):
    from .recovery_job import recover
    with connection, connection.makefile('rwb') as stream:
        _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != os.getuid(): return
        connection.settimeout(300)
        send(stream,{'type':'hello','limited':limits_available(),'pid':os.getpid()})
        request=receive(stream,65536)
        if not limits_available():
            send(stream,{'type':'error','reason':'resource_limits_unavailable'});return
        directory=Path(request['directory'])
        if not directory.is_absolute() or directory.parent.name!='events' or directory.is_symlink():
            raise ValueError('invalid event directory')
        def checkpoint(value):
            send(stream,{'type':'checkpoint','work':value})
            if receive(stream,4096).get('action')!='continue': raise InterruptedError('recovery cancelled')
        try:
            value=recover(directory,request['camera_id'],checkpoint)
            import resource
            value['work']['resource_limits_verified']=True
            value['work']['worker_peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            value['work']['decoder_peak_rss_kib']=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            send(stream,{'type':'result','payload':value})
        except Exception as error:
            send(stream,{'type':'error','reason':str(error)[:240]})


def main():
    if os.environ.get('LISTEN_PID')!=str(os.getpid()) or os.environ.get('LISTEN_FDS')!='1':
        raise SystemExit('Use the isolated magi-recovery.socket unit')
    server=socket.socket(fileno=3)
    if server.family!=socket.AF_UNIX: raise SystemExit('Unix socket required')
    while True:
        connection,_=server.accept()
        try: handle(connection)
        except (OSError,ValueError,KeyError): connection.close()


if __name__=='__main__': main()
