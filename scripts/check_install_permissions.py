"""CI-only root-owned install regression; uses a disposable temp directory."""
import os
from pathlib import Path
import subprocess
import tempfile

from update import repair_code_permissions


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this CI permission probe as root.')
    with tempfile.TemporaryDirectory(prefix='magi-permissions-') as directory:
        base = Path(directory)
        base.chmod(0o755)
        app = base / 'app'
        package = app / 'src/magi'
        previous_umask = os.umask(0o077)
        try:
            package.mkdir(parents=True)
            (package / '__init__.py').write_text('__version__ = "permissions-probe"')
            config = app / 'config.json'
            config.write_text('private test configuration')
        finally:
            os.umask(previous_umask)
        app.chmod(0o755)
        code = (f'import sys;sys.path.insert(0,{str(app / "src")!r});'
                'import magi;print(magi.__version__)')
        def as_service(source):
            return subprocess.run(['runuser', '-u', 'nobody', '--', '/usr/bin/python3',
                                   '-I', '-c', source], capture_output=True, text=True, timeout=10)
        before = as_service(code)
        assert before.returncode != 0 and 'ModuleNotFoundError' in before.stderr, before.stderr
        repair_code_permissions(app)
        after = as_service(code)
        assert after.returncode == 0 and after.stdout.strip() == 'permissions-probe', after.stderr
        private = as_service(f'open({str(config)!r}).read()')
        assert private.returncode != 0 and 'PermissionError' in private.stderr, private.stderr
        print('Root-owned private install: import failed before repair, passed afterward; config stayed private.')


if __name__ == '__main__':
    main()
