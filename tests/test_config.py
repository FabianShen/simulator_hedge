import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sim_hedge.config import load_env_file


class EnvironmentFileTests(unittest.TestCase):
    def test_finds_parent_env_and_loads_quotes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "project" / "nested"
            child.mkdir(parents=True)
            (root / ".env").write_text(
                'SIM_USERNAME="user"\nSIM_PASSWORD=secret\n', encoding="utf-8"
            )
            with patch.dict(os.environ, {}, clear=True):
                loaded = load_env_file(child)

                self.assertEqual(loaded, root / ".env")
                self.assertEqual(os.environ["SIM_USERNAME"], "user")
                self.assertEqual(os.environ["SIM_PASSWORD"], "secret")

    def test_does_not_override_shell_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("SIM_ACCOUNT_ID=file-value\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"SIM_ACCOUNT_ID": "shell-value"}, clear=True
            ):
                load_env_file(path)

                self.assertEqual(os.environ["SIM_ACCOUNT_ID"], "shell-value")

    def test_returns_none_when_no_file_exists(self) -> None:
        with TemporaryDirectory() as temporary:
            self.assertIsNone(load_env_file(Path(temporary)))


if __name__ == "__main__":
    unittest.main()
