"""Read-only once-per-second Pi recovery measurements; never enables recovery.

Run as the Magi user (or root) with access to the owner control socket.
JSONL includes raw counters, not an automatic performance pass claim.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import time


def read(path):
    try: return Path(path).read_text()[:65536]
    except OSError: return None


def status(path):
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
        connection.settimeout(.8);connection.connect(str(path))
        connection.sendall(b'{"command":"status"}\n')
        with connection.makefile('rb') as stream:
            raw=stream.readline(1024*1024+1)
            if len(raw)>1024*1024:raise ValueError('status limit')
            return json.loads(raw)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--socket',required=True,type=Path)
    parser.add_argument('--phase',required=True,choices=['A','B','A2','night-A','night-B','night-A2','soak'])
    parser.add_argument('--seconds',type=int,default=1800)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--events',type=Path,action='append',default=[],help='Camera events directory; repeat per camera')
    args=parser.parse_args()
    if not 1<=args.seconds<=86400:parser.error('seconds must be 1..86400')
    cgroup=Path('/sys/fs/cgroup/system.slice/magi-recovery.service')
    cursors={};last={};next_tick=time.monotonic()
    with args.output.open('x') as out:
        for _ in range(args.seconds):
            row={'unix':time.time(),'phase':args.phase,'host_cpu':read('/proc/stat'),
                 'memory':read('/proc/meminfo'),'diskstats':read('/proc/diskstats'),
                 'temperature_millidegrees':read('/sys/class/thermal/thermal_zone0/temp'),
                 'worker':{name:read(cgroup/name) for name in ('cpu.stat','cpu.max','memory.current','memory.peak','memory.events','memory.max','memory.swap.current','io.stat')}}
            try:row['status']=status(args.socket)
            except (OSError,ValueError) as error:row['status_error']=str(error)
            try:row['throttling']=subprocess.run(['vcgencmd','get_throttled'],capture_output=True,text=True,timeout=.5).stdout.strip()
            except (OSError,subprocess.TimeoutExpired):row['throttling']=None
            updates=[]
            for root in args.events:
                try:
                    cursor=cursors.setdefault(root, None)
                    if cursor is None:cursor=os.scandir(root);cursors[root]=cursor
                    for _ in range(100):
                        try:entry=next(cursor)
                        except StopIteration:cursor.close();cursors[root]=None;break
                        if not entry.is_dir(follow_symlinks=False):continue
                        path=Path(entry.path)/'recovery.json'
                        try:
                            info=path.stat()
                            if info.st_size>4*1024*1024 or last.get(str(path))==info.st_mtime_ns:continue
                            value=json.loads(path.read_text());last[str(path)]=info.st_mtime_ns
                            updates.append({'directory':entry.path,'recovery':value})
                        except (OSError,ValueError):pass
                except OSError:pass
            row['recovery_updates']=updates
            # Incremental scan observations, not a claim that every archive
            # job was observed each second. Preserve raw state for auditing.
            out.write(json.dumps(row,separators=(',',':'))+'\n');out.flush()
            next_tick+=1
            time.sleep(max(0,next_tick-time.monotonic()))
    for cursor in cursors.values():
        if cursor is not None:cursor.close()


if __name__=='__main__':main()
