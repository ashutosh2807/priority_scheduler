"""Resolve installation paths relative to the portal, never the shell's cwd."""
from pathlib import Path


PORTAL_DIR = Path(__file__).resolve().parent


def project_path(value, default, *, base=PORTAL_DIR):
    path = Path(str(value).strip()).expanduser() if value and str(value).strip() else Path(default)
    if not path.is_absolute():
        path = Path(base) / path
    return path.resolve()
