"""Identify portal code loaded by a running Django process, without user data."""
from hashlib import sha256
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


def current_portal_revision(base_dir=BASE_DIR):
    """Hash runtime Python code; exclude credentials, data, logs and test code.

    Static files are served from disk by runserver and do not need a process
    restart. Django's reloader clears template caches on HTML changes without
    restarting Python. Only imported Python code needs a process revision.
    """
    base_dir = Path(base_dir)
    paths = []
    for directory in ("config", "apps"):
        for path in (base_dir / directory).rglob("*.py"):
            parts = path.relative_to(base_dir).parts
            if not path.name.startswith("test") and not {"tests", "migrations"}.intersection(parts[:-1]):
                paths.append(path)
    for filename in ("project_paths.py", "portal_revision.py"):
        path = base_dir / filename
        if path.is_file():
            paths.append(path)
    digest = sha256(b"itrp-portal-revision-v1\0")
    for path in sorted(paths, key=lambda item: item.relative_to(base_dir).as_posix()):
        digest.update(path.relative_to(base_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
