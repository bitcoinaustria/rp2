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

import builtins
import unittest
from typing import Callable, cast
from unittest.mock import patch

from rp2.entry_types import TransactionType
from rp2.localization import _, set_generation_language
from rp2.plugin.report import abstract_ods_generator, rp2_full_report
from rp2.rp2_error import RP2ValueError

_ENGLISH = "en"
_SPANISH = "es"
_BUY = "buy"
_SPANISH_BUY = "compra"
_SELL = "sell"
_LEGEND = "Legend"
_TRANSLATOR = "_"


class TestLocalization(unittest.TestCase):
    def tearDown(self) -> None:
        set_generation_language(_ENGLISH)

    def test_existing_imports_and_transaction_labels_follow_language_changes(self) -> None:
        ods_translate = cast(Callable[[str], str], getattr(abstract_ods_generator, _TRANSLATOR))
        report_translate = cast(Callable[[str], str], getattr(rp2_full_report, _TRANSLATOR))
        for language, buy, sell, legend in [(_ENGLISH, _BUY, _SELL, _LEGEND), (_SPANISH, _SPANISH_BUY, "venta", "Leyenda"), (_ENGLISH, _BUY, _SELL, _LEGEND)]:
            with self.subTest(language=language):
                set_generation_language(language)
                self.assertEqual(TransactionType.BUY.get_translation(), buy)
                self.assertEqual(TransactionType.SELL.get_translation(), sell)
                self.assertEqual(_(_LEGEND), legend)
                self.assertEqual(report_translate(_LEGEND), legend)
                self.assertEqual(ods_translate(_LEGEND), legend)

    def test_selecting_language_does_not_replace_host_builtin_translation(self) -> None:
        host_translation = object()
        with patch.object(builtins, _TRANSLATOR, host_translation, create=True):
            set_generation_language(_SPANISH)
            self.assertIs(cast(object, getattr(builtins, _TRANSLATOR)), host_translation)
            self.assertEqual(_(_BUY), _SPANISH_BUY)

    def test_invalid_language_preserves_previous_translation(self) -> None:
        set_generation_language(_SPANISH)
        with self.assertRaises(RP2ValueError):
            set_generation_language("not_a_locale")
        self.assertEqual(_(_BUY), _SPANISH_BUY)


if __name__ == "__main__":
    unittest.main()
