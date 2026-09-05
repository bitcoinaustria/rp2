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
from pathlib import Path

from rp2.plugin.country.jp import JP
from rp2.plugin.report.abstract_ods_generator import AbstractODSGenerator


class _Cell:
    def __init__(self) -> None:
        self.value: object = None
        self.formula: str = ""
        self.style_name: str = ""

    def set_value(self, value: object) -> None:
        self.value = value


class _Sheet:
    def __init__(self) -> None:
        self.__cells: dict[tuple[int, int], _Cell] = {}

    def __getitem__(self, key: tuple[int, int]) -> _Cell:
        if key not in self.__cells:
            self.__cells[key] = _Cell()
        return self.__cells[key]


# pylint: disable=protected-access
class TestAbstractODSGenerator(unittest.TestCase):
    def test_jp_klingon_templates_reuse_jp_english_templates(self) -> None:
        generator = AbstractODSGenerator()
        country = JP()

        for template_name in ("open_positions", "rp2_full_report"):
            template_path = Path(generator._get_template_path(template_name, country, "kl"))
            self.assertEqual(template_path.name, f"template_{template_name}_en.ods")
            self.assertEqual(template_path.parent.name, "jp")

    def test_fill_cell_treats_raw_formula_like_text_as_literal(self) -> None:
        sheet = _Sheet()

        AbstractODSGenerator._fill_cell(sheet, 0, 0, '=HYPERLINK("https://example.test";"click")', apply_style=False)
        AbstractODSGenerator._fill_cell(sheet, 0, 1, AbstractODSGenerator._formula("=SUM(1;1)"), apply_style=False)

        self.assertEqual(sheet[0, 0].value, '=HYPERLINK("https://example.test";"click")')
        self.assertEqual(sheet[0, 0].formula, "")
        self.assertIsNone(sheet[0, 1].value)
        self.assertEqual(sheet[0, 1].formula, "=SUM(1;1)")


if __name__ == "__main__":
    unittest.main()
