"""Reject common private ML artifacts from tracked paths and source releases.

Filename-based accident prevention only; never store secrets in a public checkout.
"""
from pathlib import PurePosixPath
import subprocess

MODEL_SUFFIXES = frozenset({
    '.pt', '.pth', '.ckpt', '.safetensors', '.onnx', '.hef', '.har',
    '.h5', '.hdf5', '.keras', '.tflite', '.pb', '.pkl', '.pickle',
    '.npz', '.npy', '.bin', '.engine', '.plan', '.gguf', '.ggml',
})
PRIVATE_DIRECTORIES = frozenset({
    'private-models', 'private-training', 'private-data', 'datasets',
    'checkpoints', 'wandb', 'mlruns',
})


def blocked_paths(paths):
    blocked = []
    for name in paths:
        path = PurePosixPath(name.replace('\\', '/').lower())
        if (MODEL_SUFFIXES.intersection(path.suffixes)
                or PRIVATE_DIRECTORIES.intersection(path.parts[:-1])):
            blocked.append(name)
    return sorted(blocked)


def require_public_paths(paths):
    blocked = blocked_paths(paths)
    if blocked:
        raise ValueError('Private/model artifacts are not allowed in the public '
                         'source release:\n' + '\n'.join(blocked)
                         + '\nKeep these in separate private storage; see '
                         'docs/MODEL-POLICY.md.')


if __name__ == '__main__':
    paths = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    require_public_paths(filter(None, paths))
    print('Tracked filenames passed the public-asset check (not a content audit).')
