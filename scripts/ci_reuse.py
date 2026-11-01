"""Reuse successful PR verification only for the exact source tree being released.

The workflow records a tree-named artifact after *both* verification jobs pass.
Only artifact metadata is read; PR artifacts are never downloaded or executed.
Missing/expired evidence or API errors deliberately fall back to full checks.
"""
import json
import os
import subprocess
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class GitHub:
    def __init__(self, repository, token):
        self.base = f'https://api.github.com/repos/{repository}'
        self.token = token

    def __call__(self, path):
        request = Request(self.base + path, headers={
            'Authorization': f'Bearer {self.token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        })
        with urlopen(request, timeout=15) as response:
            return json.load(response)


def verified_run(api, workflow_id, candidates, tree):
    """Return the successful PR run that certified this tree, or None."""
    expected = 'verified-source-' + tree
    for candidate in dict.fromkeys(candidates):
        query = urlencode({'event': 'pull_request', 'head_sha': candidate,
                           'status': 'success', 'per_page': 10})
        result = api(f'/actions/workflows/{workflow_id}/runs?{query}')
        for run in result['workflow_runs']:
            if (run['event'] != 'pull_request' or run['head_sha'] != candidate
                    or run['workflow_id'] != workflow_id
                    or run['status'] != 'completed' or run['conclusion'] != 'success'):
                continue
            artifacts = api(f"/actions/runs/{run['id']}/artifacts?per_page=100")
            if any(item['name'] == expected and not item['expired']
                   for item in artifacts['artifacts']):
                return run['id']
    return None


def reuse(event, api, run_id, commit, tree, parents):
    if event != 'push':
        return None
    workflow_id = api(f'/actions/runs/{run_id}')['workflow_id']
    # Ordinary merge commits preserve the PR head as their second parent.
    # Squash/rebase/direct pushes without evidence get full verification.
    candidates = [commit] + (parents[1:2] if len(parents) == 2 else [])
    return verified_run(api, workflow_id, candidates, tree)


def main():
    run = None
    if os.environ['GITHUB_EVENT_NAME'] == 'push':
        try:
            commit = os.environ['GITHUB_SHA']
            tree = subprocess.check_output(['git', 'rev-parse', commit + '^{tree}'], text=True).strip()
            parents = subprocess.check_output(['git', 'show', '-s', '--format=%P', commit], text=True).split()
            api = GitHub(os.environ['GITHUB_REPOSITORY'], os.environ['GH_TOKEN'])
            run = reuse('push', api, os.environ['GITHUB_RUN_ID'], commit, tree, parents)
        except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError,
                subprocess.CalledProcessError) as error:
            print(f'Verification evidence unavailable ({type(error).__name__}); running full checks.')
    decision = 'true' if run is not None else 'false'
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write(f'reuse={decision}\n')
    print(f'Reusing successful PR run {run} for identical source files.' if run is not None
          else 'Full verification required.')


if __name__ == '__main__':
    main()
