import os
import runpy
import sqlite3
import tarfile
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import oracleiq
from scripts.package_release import build_archive
from scripts.sqlite_backup import copy_database


class EnvironmentTests(unittest.TestCase):
    def test_dotenv_quotes_bom_comments_and_environment_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".env").write_text(
                'ODIN_TEST_A="hello # world"\n'
                "ODIN_TEST_B=value # inline comment\n"
                "ODIN_TEST_C=file\n"
                "ODIN_TEST_D='${ODIN_TEST_C}'\n",
                encoding="utf-8-sig",
            )
            with patch.object(oracleiq, "BASE", root), patch.dict(
                os.environ, {"ODIN_TEST_C": "environment"}, clear=True
            ):
                oracleiq.load_env()
                self.assertEqual(os.environ["ODIN_TEST_A"], "hello # world")
                self.assertEqual(os.environ["ODIN_TEST_B"], "value")
                self.assertEqual(os.environ["ODIN_TEST_C"], "environment")
                self.assertEqual(os.environ["ODIN_TEST_D"], "${ODIN_TEST_C}")

    def test_import_has_no_environment_file_side_effect(self):
        with patch("dotenv.load_dotenv") as dotenv_loader:
            runpy.run_path(str(Path(__file__).parent.parent / "oracleiq.py"))
            dotenv_loader.assert_not_called()

    def test_compatibility_entry_points_delegate_to_main(self):
        for script, command in (("run.py", "all"), ("run_web_only.py", "web")):
            with self.subTest(script=script), patch("oracleiq.main", return_value=0) as main:
                with self.assertRaises(SystemExit) as result:
                    runpy.run_path(str(Path(__file__).parent.parent / script), run_name="__main__")
                self.assertEqual(result.exception.code, 0)
                main.assert_called_once_with([command])


class BackupTests(unittest.TestCase):
    def test_cli_backup_and_restore_use_explicit_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup, restored = (Path(directory) / name for name in
                                        ("source.db", "backup.db", "restored.db"))
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("CREATE TABLE samples (value INTEGER)")
                connection.execute("INSERT INTO samples VALUES (42)")
                connection.commit()
            with patch.dict(os.environ, {"ODIN_DB_PATH": str(source)}), \
                    patch.object(oracleiq, "load_env"), patch("builtins.print"):
                self.assertEqual(oracleiq.main(["backup", str(backup)]), 0)
                self.assertEqual(oracleiq.main(["restore", str(backup), str(restored)]), 0)
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM samples").fetchone()[0], 42)

    def test_live_wal_backup_and_restore_preserve_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            source, backup, restored = (Path(directory) / name for name in
                                        ("source.db", "backup.db", "restored.db"))
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE samples (value TEXT)")
                connection.execute("INSERT INTO samples VALUES ('synthetic fixture')")
                connection.commit()
                self.assertTrue(Path(str(source) + "-wal").exists())
                copy_database(source, backup)
                copy_database(backup, restored)
                connection.execute("INSERT INTO samples VALUES ('later')")
                connection.commit()
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute("SELECT value FROM samples").fetchall(),
                                 [("synthetic fixture",)])
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_existing_destination_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "source.db", Path(directory) / "existing.db"
            with closing(sqlite3.connect(source)) as connection:
                connection.execute("CREATE TABLE samples (id INTEGER)")
            destination.write_bytes(b"keep this file")
            with self.assertRaises(FileExistsError):
                copy_database(source, destination)
            self.assertEqual(destination.read_bytes(), b"keep this file")
            with self.assertRaises(ValueError):
                copy_database(source, source)

    def test_invalid_or_missing_source_does_not_leave_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, destination = Path(directory) / "invalid.db", Path(directory) / "copy.db"
            with self.assertRaises(FileNotFoundError):
                copy_database(source, destination)
            source.write_bytes(b"not a SQLite database")
            with self.assertRaises(sqlite3.DatabaseError):
                copy_database(source, destination)
            self.assertFalse(destination.exists())


class PackagingTests(unittest.TestCase):
    def test_allowlist_excludes_local_data_and_keeps_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            expected = ["README.md", ".env.example", "api/app.py", "static/vendor/purify.js"]
            forbidden = [".env", ".env.local", "copy.sqlite", "oracleiq.db", "debug.log",
                         "api/.env.py", "api/private.db", "static/private.log",
                         "tests/__pycache__/cache.pyc", "backups/copy.py", ".git/config"]
            for name in expected + forbidden:
                path = root / Path(name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture", encoding="utf-8")
            output = Path(directory) / "release.tar.gz"
            self.assertEqual(build_archive(root, output), len(expected))
            with tarfile.open(output) as archive:
                self.assertEqual(set(archive.getnames()),
                                 {"oracleiq/" + name for name in expected})
            with self.assertRaises(FileExistsError):
                build_archive(root, output)

    @unittest.skipIf(os.name == "nt", "Symlink creation requires optional Windows privileges")
    def test_symlinks_cannot_include_external_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            root.mkdir()
            (root / "README.md").write_text("fixture", encoding="utf-8")
            external = Path(directory) / "external"
            external.mkdir()
            (external / "secret.py").write_text("synthetic", encoding="utf-8")
            (root / "api").symlink_to(external, target_is_directory=True)
            output = Path(directory) / "release.tar.gz"
            self.assertEqual(build_archive(root, output), 1)
