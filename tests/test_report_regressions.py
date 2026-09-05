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

# ezodf exposes dynamically typed sheet and cell objects.
# mypy: disallow-any-explicit=False, disallow-any-expr=False

import tempfile
import unittest
from pathlib import Path
from typing import Any, List, Optional

import ezodf
from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MAX_DATE, MIN_DATE, Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.fifo import AccountingMethod
from rp2.plugin.country.ie import IE
from rp2.plugin.country.jp import JP
from rp2.plugin.report.ie.tax_report_ie import Generator as IrelandGenerator
from rp2.plugin.report.jp.tax_report_jp import Generator as JapanGenerator
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET = "B1"
_EXCHANGE = "Coinbase"
_HOLDER = "Bob"
_CONFIGURATION_PATH = "./config/test_data.ini"
_METHOD_NAME = "fifo"
_OUTPUT_PREFIX = ""
_ACQUISITION_UNIT_COST = RP2Decimal("100")


class TestReportRegressions(unittest.TestCase):
    @staticmethod
    def _buy(configuration: Configuration, year: int, row: int) -> InTransaction:
        return InTransaction(configuration, f"{year}-01-01 12:00:00 +0000", _ASSET, _EXCHANGE, _HOLDER, "BUY", _ACQUISITION_UNIT_COST, RP2Decimal("2"), row=row)

    @staticmethod
    def _disposal(configuration: Configuration, year: int, kind: str, proceeds: Optional[RP2Decimal] = None) -> OutTransaction:
        return OutTransaction(
            configuration,
            f"{year}-06-01 12:00:00 +0000",
            _ASSET,
            _EXCHANGE,
            _HOLDER,
            kind,
            RP2Decimal("200"),
            RP2Decimal("1"),
            ZERO,
            fiat_out_no_fee=proceeds,
            row=10,
        )

    @staticmethod
    def _compute(configuration: Configuration, buys: List[InTransaction], disposal: OutTransaction) -> ComputedData:
        sets: List[TransactionSet] = []
        entries_by_kind: List[tuple[str, List[AbstractTransaction]]] = [("IN", list(buys)), ("OUT", [disposal]), ("INTRA", [])]
        for kind, entries in entries_by_kind:
            transactions = TransactionSet(configuration, kind, _ASSET)
            for entry in entries:
                transactions.add_entry(entry)
            sets.append(transactions)
        methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        methods.insert_node(MIN_DATE.year, AccountingMethod())
        return compute_tax(configuration, AccountingEngine(methods), InputData(_ASSET, sets[0], sets[1], sets[2]))

    def test_ireland_lost_disposal_keeps_basis_and_recovery_proceeds(self) -> None:
        country = IE()
        configuration = Configuration(_CONFIGURATION_PATH, country)
        for proceeds in (None, RP2Decimal("50")):
            with self.subTest(proceeds=proceeds), tempfile.TemporaryDirectory() as output_dir:
                computed = self._compute(configuration, [self._buy(configuration, 2020, 1)], self._disposal(configuration, 2020, "LOST", proceeds))
                IrelandGenerator().generate(country, {MIN_DATE.year: _METHOD_NAME}, {_ASSET: computed}, output_dir, _OUTPUT_PREFIX, MIN_DATE, MAX_DATE, "en_IE")
                report: Any = ezodf.opendoc(str(Path(output_dir) / f"{_METHOD_NAME}_{IrelandGenerator.OUTPUT_FILE}"))
                sheet = report.sheets["Investment Expenses"]
                row = IrelandGenerator.HEADER_ROWS
                self.assertEqual(sheet[row, 4].value, float(proceeds or ZERO))
                self.assertEqual(sheet[row, 5].value, 100)
                self.assertEqual(sheet[row, 8].value, float((proceeds or ZERO) - _ACQUISITION_UNIT_COST))
                self.assertEqual(sheet[row, 9].value, "OUT / LOST")

    def test_japan_carries_last_existing_year_in_chronological_order(self) -> None:
        country = JP()
        configuration = Configuration(_CONFIGURATION_PATH, country)
        computed = self._compute(
            configuration, [self._buy(configuration, 2020, 1), self._buy(configuration, 2023, 2)], self._disposal(configuration, 2022, "SELL")
        )
        with tempfile.TemporaryDirectory() as output_dir:
            JapanGenerator().generate(country, {MIN_DATE.year: _METHOD_NAME}, {_ASSET: computed}, output_dir, _OUTPUT_PREFIX, MIN_DATE, MAX_DATE, "en")
            report: Any = ezodf.opendoc(str(Path(output_dir) / f"{_METHOD_NAME}_{JapanGenerator.OUTPUT_FILE}"))
            self.assertEqual([name for name in report.sheets.names() if name.startswith(f"{_ASSET}_")], [f"{_ASSET}_{year}" for year in (2020, 2022, 2023)])
            for year, previous_year in ((2022, 2020), (2023, 2022)):
                sheet = report.sheets[f"{_ASSET}_{year}"]
                # Each synthetic year has one detail row: carry-in quantity and basis are E31/E32.
                self.assertEqual(sheet["E31"].formula, f"='{_ASSET}_{previous_year}'.I31")
                self.assertEqual(sheet["E32"].formula, f"='{_ASSET}_{previous_year}'.I32")


if __name__ == "__main__":
    unittest.main()
