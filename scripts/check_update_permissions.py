"""CI root probe: real Unix peers and root-owned helper files, in a temp directory."""
import json
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import threading

from install_update_agent import install
from update_agent import Agent, Handler, Server


def main():
    if os.geteuid()!=0:
        raise SystemExit('Run this CI permission probe as root.')
    with tempfile.TemporaryDirectory(prefix='magi-update-permissions-') as directory:
        root=Path(directory);root.chmod(0o755)
        install(Path(__file__).resolve().parents[1],root/'installed',lambda *a,**k:None)
        state=root/'private-state'
        agent=Agent(state,current=lambda:'0.14.1',choose=lambda selection:{'tag_name':'v0.15.0'})
        path=root/'test.sock';server=Server(str(path),Handler)
        server.agent=agent;server.allowed_uid=pwd.getpwnam('nobody').pw_uid
        # Deliberately permissive fixture: SO_PEERCRED must reject a different UID.
        path.chmod(0o666)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def as_user(user,code):
            return subprocess.run(['runuser','-u',user,'--','/usr/bin/python3','-I','-c',code],capture_output=True,text=True,timeout=10)
        def request(user,payload):
            code=('import socket,json; s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(3);'
                  f's.connect({str(path)!r});s.sendall({(json.dumps(payload)+chr(10)).encode()!r});'
                  'print(s.recv(8192).decode());s.close()')
            result=as_user(user,code)
            assert result.returncode==0,result.stderr
            return json.loads(result.stdout)
        try:
            assert request('nobody',{'action':'status'})['helper_available']
            assert 'error' in request('daemon',{'action':'check'})
            assert 'error' in request('nobody',{'action':'check','command':'id'})
            assert 'error' in request('nobody',{'action':'install','version':'/tmp/evil'})
            helper=root/'installed/usr/local/lib/magi/update_agent.py'
            assert 'PermissionError' in as_user('nobody',f'open({str(helper)!r},"w").write("bad")').stderr
            assert 'PermissionError' in as_user('nobody',f'open({str(state)!r}+"/ui-status.json").read()').stderr
            print('Update helper: approved Unix peer allowed; other UID and arbitrary commands rejected; root files and state stayed private.')
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':
    main()
