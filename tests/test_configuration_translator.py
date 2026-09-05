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

import json
import subprocess
import sys
import unittest
from configparser import ConfigParser
from pathlib import Path
from tempfile import TemporaryDirectory

from rp2.configuration import Configuration, Keyword
from rp2.plugin.country.us import US

_ENCODING = "utf-8"
_METHOD = "fifo"


class TestConfigurationTranslator(unittest.TestCase):
    def test_generator_lists_survive_json_to_ini_conversion(self) -> None:
        for generators in (None, [], ["rp2.plugin.report.open_positions", "rp2.plugin.report.rp2_full_report"]):
            with self.subTest(generators=generators), TemporaryDirectory() as directory:
                base = ConfigParser()
                base.read("./config/test_data.ini")
                legacy: dict[str, object] = {
                    field: [value.strip() for value in base["general"][field].split(",")] for field in ("assets", "exchanges", "holders")
                }
                for section in ("in_header", "out_header", "intra_header"):
                    legacy[section] = {field: int(column) for field, column in base[section].items()}
                legacy["accounting_methods"] = {"2020": _METHOD}
                if generators is not None:
                    legacy[Keyword.GENERATORS.value] = generators
                source = Path(directory) / "legacy.json"
                destination = Path(directory) / "converted.ini"
                source_contents = json.dumps(legacy)
                source.write_text(source_contents, encoding=_ENCODING)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from rp2.rp2_configuration_translator import rp2_configuration_translator; rp2_configuration_translator()",
                        str(source),
                        "-o",
                        str(destination),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                configuration = Configuration(str(destination), US())
                expected = {f"rp2.plugin.report.{name}" for name in US().get_report_generators()} if generators is None else set(generators)
                self.assertEqual(configuration.generators, expected)
                expected_methods: dict[int, str] = {2020: _METHOD}
                self.assertEqual(configuration.years_2_accounting_method_names, expected_methods)
                self.assertEqual(source.read_text(encoding=_ENCODING), source_contents)


if __name__ == "__main__":
    unittest.main()
