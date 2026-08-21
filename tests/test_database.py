import tempfile
import unittest
from pathlib import Path

from database.db import Database


class DatabaseTests(unittest.TestCase):
    def test_configured_database_path_and_pragmas(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            database = Database(str(path))
            database.init_db()
            with database.connect() as connection:
                foreign_keys = connection.execute(
                    "PRAGMA foreign_keys"
                ).fetchone()[0]
                busy_timeout = connection.execute(
                    "PRAGMA busy_timeout"
                ).fetchone()[0]

            self.assertEqual(database.db_path, path.resolve())
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(busy_timeout, 10000)


if __name__ == "__main__":
    unittest.main()
