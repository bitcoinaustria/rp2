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

import sys
import unittest
from typing import Set

from rp2.plugin.country.at import AT, rp2_entry


class TestPluginCountryAT(unittest.TestCase):
    def setUp(self) -> None:
        self.country = AT()

    def test_iso_codes(self) -> None:
        self.assertEqual(self.country.country_iso_code, "at")
        self.assertEqual(self.country.currency_iso_code, "eur")

    def test_long_term_capital_gain_period_is_disabled(self) -> None:
        # Austria has no generic day-threshold; regime-specific handling lives in the
        # Austrian accounting method (Phase 3+).
        self.assertEqual(self.country.get_long_term_capital_gain_period(), sys.maxsize)

    def test_accounting_methods(self) -> None:
        expected: Set[str] = {"fifo", "moving_average", "moving_average_at"}
        self.assertEqual(self.country.get_accounting_methods(), expected)
        self.assertEqual(self.country.get_default_accounting_method(), "moving_average_at")

    def test_report_generators(self) -> None:
        # rp2_full_report is intentionally excluded: AT disables the day-threshold for
        # long/short (sys.maxsize), so the generic report would collapse Altvermögen into a
        # single short-term bucket and mislead taxpayers.
        expected: Set[str] = {"open_positions", "at.tax_report_at"}
        self.assertEqual(self.country.get_report_generators(), expected)

    def test_default_generation_language(self) -> None:
        self.assertEqual(self.country.get_default_generation_language(), "de_AT")

    def test_entry_point_is_callable(self) -> None:
        # Verifies the console-script target resolves and is wired to rp2_main.
        self.assertTrue(callable(rp2_entry))


if __name__ == "__main__":
    unittest.main()
