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

import unittest
from configparser import ConfigParser
from importlib.metadata import metadata
from pathlib import Path

ROOT_PATH: Path = Path(__file__).resolve().parent.parent

_PYTHON_REQUIRES: str = "python_requires"
_INCLUDE_PACKAGE_DATA: str = "include_package_data"
_ZIP_SAFE: str = "zip_safe"
_OPTIONS_SECTION: str = "options"
_PACKAGE_FIND_SECTION: str = "options.packages.find"


class TestPackaging(unittest.TestCase):
    def test_python_requires_is_emitted_in_package_metadata(self) -> None:
        self.assertEqual(metadata("rp2")["Requires-Python"], ">=3.10")

    def test_setuptools_options_are_not_declared_as_package_finder_options(self) -> None:
        setup_config = ConfigParser()
        setup_config.read(ROOT_PATH / "setup.cfg")
        for option in (_PYTHON_REQUIRES, _INCLUDE_PACKAGE_DATA, _ZIP_SAFE):
            self.assertIn(option, setup_config[_OPTIONS_SECTION])
            self.assertNotIn(option, setup_config[_PACKAGE_FIND_SECTION])

    def test_archive_target_uses_git_archive_instead_of_working_tree_zip(self) -> None:
        makefile = (ROOT_PATH / "Makefile").read_text(encoding="utf-8")
        self.assertIn("\tgit archive --format=zip --output=rp2.zip HEAD", makefile)
        self.assertNotIn("\tzip -r rp2.zip .", makefile)


if __name__ == "__main__":
    unittest.main()
