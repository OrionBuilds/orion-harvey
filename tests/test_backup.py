import sqlite3
import tarfile
from datetime import datetime, timedelta

from harvey.backup import make_backup, prune


def _project(tmp_path):
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    (root / "skills" / "persona").mkdir(parents=True)
    (root / "harvey" / "__pycache__").mkdir(parents=True)
    (root / "harvey" / "main.py").write_text("print('hi')")
    (root / "harvey" / "__pycache__" / "x.pyc").write_text("junk")
    (root / "skills" / "persona" / "identity.md").write_text("Jordan")
    (root / "harvey.local.yaml").write_text("persona: {}")
    (root / ".env").write_text("SECRET=1")
    (root / "data" / "ms_token.json").write_text("{}")
    db = sqlite3.connect(root / "data" / "harvey.db")
    db.execute("create table t(x)"); db.execute("insert into t values (42)"); db.commit(); db.close()
    return root


def test_backup_has_a_readable_database_the_work_and_no_secrets(tmp_path):
    root = _project(tmp_path)
    out = make_backup(root, tmp_path / "bk", now=datetime(2026, 11, 1, 3, 15))
    assert out.name == "harvey-20261101-031500.tar.gz" and oct(out.stat().st_mode & 0o777) == "0o600"
    with tarfile.open(out) as tar:
        names = tar.getnames()
        assert "harvey/skills/persona/identity.md" in names and "harvey/harvey.local.yaml" in names
        assert "harvey/data/harvey.db" in names
        assert not any(n.endswith(".env") or "ms_token" in n or "__pycache__" in n for n in names)
        tar.extract("harvey/data/harvey.db", tmp_path / "restore", filter="data")
    assert sqlite3.connect(tmp_path / "restore" / "harvey" / "data" / "harvey.db").execute("select x from t").fetchone() == (42,)


def test_only_the_newest_fourteen_are_kept_and_other_files_are_untouched(tmp_path):
    d = tmp_path / "bk"
    d.mkdir()
    for i in range(20):
        (d / f"harvey-202611{i + 1:02d}-031500.tar.gz").write_text("x")
    (d / "notes.txt").write_text("mine")
    assert len(prune(d, 14)) == 6
    assert len(list(d.glob("harvey-*.tar.gz"))) == 14 and (d / "notes.txt").exists()
    assert not (d / "harvey-20261101-031500.tar.gz").exists()
