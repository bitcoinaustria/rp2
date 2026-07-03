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

from rp2.plugin.country.jp import JP, rp2_entry


class TestPluginCountryJP(unittest.TestCase):
    def test_jp_plugin_does_not_advertise_fifo_placeholder(self) -> None:
        country = JP()
        self.assertEqual(country.get_default_accounting_method(), "")
        self.assertFalse(country.get_accounting_methods())
        self.assertFalse(country.get_report_generators())

    def test_jp_entry_point_fails_loudly(self) -> None:
        with self.assertRaises(SystemExit) as context:
            rp2_entry()
        self.assertIn("Japan support is disabled", str(context.exception))
        self.assertIn("FIFO wiring was an incorrect placeholder", str(context.exception))


if __name__ == "__main__":
    unittest.main()
