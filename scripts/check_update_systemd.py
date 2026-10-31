"""CI root integration: preflight and health probes inside helper hardening."""
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile

from update_agent import ENV


def main():
    if os.geteuid()!=0:
        raise SystemExit('Run this integration probe as root.')
    # This check intentionally requires a real system manager, rather than
    # silently substituting a normal shell for the deployed security context.
    subprocess.run(['systemctl','show','--property=Version','--value'],check=True,timeout=10)
    created=False
    try:
        pwd.getpwnam('magi')
    except KeyError:
        subprocess.run(['useradd','--system','--user-group','--no-create-home',
                        '--home-dir','/nonexistent','--shell','/usr/sbin/nologin','magi'],check=True)
        created=True
    try:
        # /tmp and /home are deliberately unavailable in the service context.
        with tempfile.TemporaryDirectory(prefix='magi-update-systemd-test-',dir='/opt') as folder:
            root=Path(folder);root.chmod(0o755)
            source=Path(__file__).resolve().parents[1]
            shutil.copyfile(source/'scripts/update.py',root/'update.py')
            (root/'update.py').chmod(0o644)
            package=root/'live/src/magi';package.mkdir(parents=True,mode=0o755)
            for parent in (package.parent,package.parent.parent,package):parent.chmod(0o755)
            (package/'__init__.py').write_text('__version__ = "fixture-version"\n')
            (package/'__init__.py').chmod(0o644)
            (package/'__main__.py').write_text('import json,os;print(json.dumps({"version":"fixture-version","uid":os.getuid()}))\n')
            (package/'__main__.py').chmod(0o644)
            expected=pwd.getpwnam('magi').pw_uid
            script=root/'probe.py'
            script.write_text(f'''import json,sys
sys.path.insert(0,{str(root)!r})
from update import Updater
updater=Updater(live={str(root/'live')!r})
assert updater.read_installed_version()=='fixture-version'
identity=updater.service_user_python(updater.live,'-c','import os;print(os.getuid())').strip()
assert int(identity)=={expected}
status=json.loads(updater.service_user_python(updater.live,'-m','magi','status','--config','unused-fixture'))
assert status=={{'version':'fixture-version','uid':{expected}}}
print('Service-user imports and JSON status checks passed inside the hardened root-helper context.')
''')
            script.chmod(0o644)
            # Exercise the actual hardening properties, then start the exact
            # subprocess from the same minimal environment as update_agent.
            settings=[]
            for line in (source/'systemd/magi-update-agent.service').read_text().splitlines():
                if line.startswith(('NoNewPrivileges=','PrivateTmp=','ProtectHome=','ProtectSystem=',
                                    'RestrictAddressFamilies=','LockPersonality=','UMask=','User=','Group=')):
                    settings.append('--property='+line)
            command=['systemd-run','--quiet','--wait','--pipe','--collect','--service-type=exec',
                     '--property=RuntimeMaxSec=120s','--property=TimeoutStopSec=5s',
                     '--property=ReadWritePaths='+str(root),*settings,
                     '/usr/bin/env','-i',*[key+'='+value for key,value in ENV.items()],
                     '/usr/bin/python3','-I',str(script)]
            result=subprocess.run(command,capture_output=True,text=True,timeout=150)
            assert result.returncode==0,result.stdout+'\n'+result.stderr
            assert 'passed inside the hardened' in result.stdout,result.stdout
            print(result.stdout.strip())
    finally:
        if created:
            subprocess.run(['userdel','magi'],check=True)
            # userdel normally removes a matching empty user-private group.
            import grp
            try:grp.getgrnam('magi')
            except KeyError:pass
            else:subprocess.run(['groupdel','magi'],check=True)


if __name__=='__main__':
    main()
