"""Nightly backup of everything that is Harvey's own and can't be re-created:
the database (a consistent SQLite snapshot, taken while he runs), the config, the
persona files and prompts, and the code. Secrets are left out on purpose: .env, OAuth
tokens and the dashboard token live only on this machine, so a stolen backup is not
a stolen identity. Re-enter keys and run `harvey microsoft login` / `harvey os login`
after a restore.
"""

import logging
import os
import sqlite3
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.backup")

BACKUP_DIR = Path.home() / "backups" / "harvey"
KEEP = 14
INCLUDE = ("harvey", "skills", "prompts", "tests", "scripts", "harvey.yaml", "harvey.local.yaml",
           "pyproject.toml", "requirements.txt", "CLAUDE.md", "README.md")
SKIP_DIRS = {"__pycache__", ".venv", ".pytest_cache", ".git", "node_modules"}


def snapshot_db(source: Path, dest: Path) -> None:
    """A consistent copy even while Harvey is writing (SQLite's online backup, not cp)."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _filter(info: tarfile.TarInfo):
    return None if any(part in SKIP_DIRS for part in Path(info.name).parts) else info


def make_backup(root: Path = PROJECT_ROOT, dest_dir: Path = BACKUP_DIR, keep: int = KEEP,
                now: datetime | None = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    out = dest_dir / f"harvey-{stamp}.tar.gz"
    with tempfile.TemporaryDirectory() as tmp:
        db_copy = Path(tmp) / "harvey.db"
        db = root / "data" / "harvey.db"
        if db.exists():
            snapshot_db(db, db_copy)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar:
            for name in INCLUDE:
                path = root / name
                if path.exists():
                    tar.add(path, arcname=f"harvey/{name}", filter=_filter)
            if db_copy.exists():
                tar.add(db_copy, arcname="harvey/data/harvey.db")
            analytics = root / "data" / "analytics.json"
            if analytics.exists():
                tar.add(analytics, arcname="harvey/data/analytics.json")
    prune(dest_dir, keep)
    logger.info("Backup written: %s", out)
    return out


def prune(dest_dir: Path, keep: int = KEEP) -> list[Path]:
    """Keep the newest ``keep`` backups; remove only files this module made."""
    files = sorted(dest_dir.glob("harvey-*.tar.gz"))
    doomed = files[:-keep] if keep > 0 else files
    for f in doomed:
        f.unlink()
    return doomed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(make_backup())
