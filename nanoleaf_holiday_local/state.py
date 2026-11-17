import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path


def state_root():
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home()/'.local/state').expanduser()
    root = base/'nanoleaf-holiday-local'
    if not root.is_absolute() or root.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Set XDG_STATE_HOME to an absolute directory outside the checkout')
    return root


def private_dir(path):
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    st = path.lstat()
    if path.is_symlink() or not path.is_dir() or st.st_uid != os.getuid():
        raise ValueError('Unexpected private directory owner or type')
    if st.st_mode & 0o077:
        raise ValueError(f'Private directory permissions too broad: {path}')


def read_private(path):
    st = path.lstat()
    if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise ValueError('Unsafe credential file permissions or type')
    return path.read_bytes()


def write_private(path, data, replace=False):
    private_dir(path.parent)
    if not isinstance(data, bytes):
        data = json.dumps(data, indent=2).encode() + b'\n'
    if replace and path.exists():
        read_private(path)  # Validate the existing file before replacing it.
    tmp = path.with_name(path.name + '.new')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)  # Atomic, fails if the destination already exists.
            tmp.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


@contextmanager
def locked(root):
    private_dir(root)
    fd = os.open(root/'setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
