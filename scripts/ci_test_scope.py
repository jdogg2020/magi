"""Reuse expensive tests only with successful evidence for their exact inputs."""
import ast
import hashlib
import json
import os
from pathlib import Path
import tomllib
import unittest

from ci_reuse import GitHub

GROUPS = {'solver': 'test_star_solve', 'calibration': 'test_auto_calibration'}
ROOT = Path(__file__).resolve().parents[1]


def inputs(root, group):
    common = {p.relative_to(root).as_posix() for pattern in (
        '.github/workflows/*', '.github/actions/**/*', 'requirements*', 'systemd/*',
        'src/magi/data/**/*', 'scripts/ci*.py', 'tests/test_ci*.py')
        for p in root.glob(pattern) if p.is_file()}
    common.update({'pyproject.toml', 'src/magi/__init__.py', f'tests/{GROUPS[group]}.py'})
    if group == 'calibration':
        # Application/CLI exercise many runtime modules; deliberately conservative.
        common.update(p.relative_to(root).as_posix() for p in (root/'src').rglob('*.py'))
        common.update(p.relative_to(root).as_posix() for p in (root/'scripts').rglob('*.py'))
    else:
        # Follow local imports, including imports nested inside functions.
        pending = [f'tests/{GROUPS[group]}.py']
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            common.add(path)
            tree = ast.parse((root/path).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == '__import__'
                    or isinstance(node.func, ast.Attribute) and node.func.attr == 'import_module'):
                    raise ValueError('Dynamic dependency needs full verification')
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    prefix = 'magi.' if node.level else ''
                    if node.level > 1:
                        raise ValueError('Unrecognized relative dependency')
                    modules = [prefix + (node.module or '')]
                    modules += [prefix + ((node.module + '.') if node.module else '') + alias.name for alias in node.names]
                for module in modules:
                    if module.startswith('magi.'):
                        candidate = 'src/' + module.replace('.', '/') + '.py'
                        if (root/candidate).is_file():
                            pending.append(candidate)
    return sorted(common)


def fingerprint(root, group):
    digest = hashlib.sha256()
    for path in inputs(root, group):
        data = (root/path).read_bytes()
        # A release version alone does not change astronomy behavior. All other
        # package metadata and initializer statements remain in the fingerprint.
        if path == 'pyproject.toml':
            metadata = tomllib.loads(data.decode())
            metadata.get('project', {}).pop('version', None)
            data = json.dumps(metadata, sort_keys=True).encode()
        elif path == 'src/magi/__init__.py':
            tree = ast.parse(data)
            tree.body = [node for node in tree.body if not (
                isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == '__version__'
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))]
            data = ast.dump(tree).encode()
        digest.update(path.encode() + b'\0' + data + b'\0')
    return digest.hexdigest()


def proof(api, workflow_id, repository, name):
    from urllib.parse import urlencode
    artifacts = api('/actions/artifacts?' + urlencode({'name': name, 'per_page': 100}))['artifacts']
    for item in artifacts:
        if item['name'] != name or item['expired']:
            continue
        origin = item.get('workflow_run') or {}
        if not origin.get('id'):
            continue
        run = api(f"/actions/runs/{origin['id']}")
        if (run['workflow_id'] == workflow_id and run['status'] == 'completed'
                and run['conclusion'] == 'success' and run['event'] in ('pull_request', 'push')
                and run['head_sha'] == origin.get('head_sha')
                and run['head_repository']['full_name'] == repository):
            return run['id']
    return None


def plan():
    values = {}
    force = os.environ.get('FORCE_FULL') == 'true'
    for group in GROUPS:
        values[group + '_reuse'] = 'false'
        values[group + '_fingerprint'] = ''
        try:
            digest = fingerprint(ROOT, group)
            values[group + '_fingerprint'] = digest
            if force:
                continue
            api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
            workflow_id = api('/actions/runs/' + os.environ['GITHUB_RUN_ID'])['workflow_id']
            run = proof(api, workflow_id, os.environ['GITHUB_REPOSITORY'], f'verified-{group}-{digest}')
            if run is not None:
                values[group + '_reuse'] = 'true'
                print(f'{group}: reuse successful run {run}; input fingerprint {digest}')
        except Exception as error:
            print(f'{group}: evidence unavailable ({type(error).__name__}); running tests.')
        if values[group + '_reuse'] == 'false':
            print(f'{group}: full tests required.')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in values.items():
            output.write(f'{key}={value}\n')


def cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from cases(test)
        else:
            yield test


def select_tests(suite, reused):
    return unittest.TestSuite(test for test in cases(suite) if test.__class__.__module__ not in reused)


def run_tests():
    skipped = {module for group, module in GROUPS.items()
               if os.environ.get(group.upper() + '_REUSE') == 'true'}
    suite = unittest.TestLoader().discover(str(ROOT/'tests'))
    selected = select_tests(suite, skipped)
    print('Reused expensive test modules: ' + (', '.join(sorted(skipped)) or 'none'), flush=True)
    return 0 if unittest.TextTestRunner(verbosity=2).run(selected).wasSuccessful() else 1


if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['run']:
        raise SystemExit(run_tests())
    plan()
