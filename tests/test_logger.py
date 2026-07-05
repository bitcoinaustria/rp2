# Copyright 2026 bitcoinaustria
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class TestLogger(unittest.TestCase):
    def test_importing_rp2_module_does_not_create_cwd_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            env = os.environ.copy()
            src_path: str = str(Path(__file__).resolve().parents[1] / "src")
            env["PYTHONPATH"] = src_path if "PYTHONPATH" not in env else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"

            completed = subprocess.run(
                [sys.executable, "-c", "import rp2.balance"],
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((Path(cwd) / "log").exists())


if __name__ == "__main__":
    unittest.main()
