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
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import ezodf
from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.moving_average_at import AccountingMethod
from rp2.plugin.country.at import AT
from rp2.plugin.report.at.tax_report_at import Generator
from rp2.rp2_decimal import RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET: str = "B1"


def _rp2_decimal(value: str) -> RP2Decimal:
    return RP2Decimal(value)


class TestTaxReportAT(unittest.TestCase):
    _configuration: Configuration
    _output_dir: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", AT())

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name
        self._output_dir = tempfile.mkdtemp(prefix="rp2-at-report-")

    def tearDown(self) -> None:
        shutil.rmtree(self._output_dir, ignore_errors=True)

    # ------------------------------------------------------------------ helpers ----

    def _make_engine(self) -> AccountingEngine:
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree[int, AbstractAccountingMethod]()
        years_2_methods.insert_node(MIN_DATE.year, AccountingMethod())
        return AccountingEngine(years_2_methods)

    def _buy(self, row: int, timestamp: str, crypto_in: str, spot_price: str, notes: Optional[str] = None) -> InTransaction:
        return InTransaction(
            self._configuration,
            timestamp,
            _ASSET,
            "Coinbase",
            "Bob",
            "BUY",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_in),
            fiat_fee=_rp2_decimal("0"),
            row=row,
            notes=notes,
        )

    def _sell(self, row: int, timestamp: str, crypto_out: str, spot_price: str, notes: Optional[str] = None) -> OutTransaction:
        return OutTransaction(
            self._configuration,
            timestamp,
            _ASSET,
            "Coinbase",
            "Bob",
            "SELL",
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_out),
            _rp2_decimal("0"),
            row=row,
            notes=notes,
        )

    def _compute(self, in_txs: List[InTransaction], out_txs: List[OutTransaction]) -> Dict[str, ComputedData]:
        in_set: TransactionSet = TransactionSet(self._configuration, "IN", _ASSET)
        for in_tx in in_txs:
            in_set.add_entry(in_tx)
        out_set: TransactionSet = TransactionSet(self._configuration, "OUT", _ASSET)
        for out_tx in out_txs:
            out_set.add_entry(out_tx)
        intra_set: TransactionSet = TransactionSet(self._configuration, "INTRA", _ASSET)
        input_data: InputData = InputData(_ASSET, in_set, out_set, intra_set)
        computed: ComputedData = compute_tax(self._configuration, self._make_engine(), input_data)
        return {_ASSET: computed}

    def _run_generator(self, asset_to_computed: Dict[str, ComputedData]) -> Path:
        Generator().generate(
            country=AT(),
            years_2_accounting_method_names={MIN_DATE.year: "moving_average_at"},
            asset_to_computed_data=asset_to_computed,
            output_dir_path=self._output_dir,
            output_file_prefix="test_",
            from_date=date(2023, 1, 1),
            to_date=date(2023, 12, 31),
            generation_language="en",
        )
        files: List[str] = [f for f in os.listdir(self._output_dir) if f.endswith(".ods")]
        self.assertEqual(len(files), 1, f"expected exactly one ODS output, found {files}")
        return Path(self._output_dir) / files[0]

    @staticmethod
    def _sheet_cell(doc: Any, sheet_name: str, row: int, col: int) -> Any:
        return doc.sheets[sheet_name][row, col].value

    def _find_row_with_label(self, doc: Any, sheet_name: str, label_fragment: str, label_col: int = 0) -> int:
        sheet: Any = doc.sheets[sheet_name]
        for row_index in range(sheet.nrows()):
            cell = sheet[row_index, label_col].value
            if isinstance(cell, str) and label_fragment in cell:
                return row_index
        raise AssertionError(f"Row with label fragment {label_fragment!r} not found in sheet {sheet_name!r}")

    # ------------------------------------------------------------------ tests ------

    def test_output_file_created_and_has_all_sheets(self) -> None:
        computed = self._compute(
            in_txs=[self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[self._sell(row=2, timestamp="2023-06-01 00:00:00 +0000", crypto_out="0.5", spot_price="300")],
        )
        path: Path = self._run_generator(computed)
        self.assertTrue(path.exists())
        doc: Any = ezodf.opendoc(str(path))
        sheet_names = set(doc.sheets.names())
        self.assertEqual(
            sheet_names,
            {
                "Summary",
                "FinanzOnline",
                "Neuvermoegen Disposals",
                "Altvermoegen Disposals",
                "Swaps (Neu, tax-neutral)",
                "Income (Kz 172)",
            },
        )

    def test_neuvermoegen_gain_on_kz_174(self) -> None:
        # Pool (2, 400, avg 200). Sell 0.5 @ 500 → cost_basis 100, proceeds 250, gain 150.
        computed = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
            ],
            out_txs=[self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500")],
        )
        path: Path = self._run_generator(computed)
        doc: Any = ezodf.opendoc(str(path))
        row: int = self._find_row_with_label(doc, "FinanzOnline", "174")
        value = self._sheet_cell(doc, "FinanzOnline", row, 2)
        self.assertEqual(value, "150.00")

    def test_neuvermoegen_loss_as_positive_magnitude_on_kz_176(self) -> None:
        # Buys at 500 & 500 → avg 500. Sell 0.5 @ 100 → loss 200 → Kz 176 = 200.00 (positive).
        computed = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="500"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="500"),
            ],
            out_txs=[self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="100")],
        )
        path: Path = self._run_generator(computed)
        doc: Any = ezodf.opendoc(str(path))
        row: int = self._find_row_with_label(doc, "FinanzOnline", "176")
        value = self._sheet_cell(doc, "FinanzOnline", row, 2)
        self.assertEqual(value, "200.00")

    def test_swap_excluded_from_kz_totals_but_listed_in_swaps_sheet(self) -> None:
        computed = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
            ],
            out_txs=[
                self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_swap_link=swap-X"),
            ],
        )
        path: Path = self._run_generator(computed)
        doc: Any = ezodf.opendoc(str(path))
        # Kz 174/176 must be 0 — swap contributes nothing taxable.
        row_174: int = self._find_row_with_label(doc, "FinanzOnline", "174")
        self.assertEqual(self._sheet_cell(doc, "FinanzOnline", row_174, 2), "0.00")
        row_176: int = self._find_row_with_label(doc, "FinanzOnline", "176")
        self.assertEqual(self._sheet_cell(doc, "FinanzOnline", row_176, 2), "0.00")
        # Swap appears on the Swaps sheet with its id extracted.
        swaps_sheet: Any = doc.sheets["Swaps (Neu, tax-neutral)"]
        self.assertEqual(swaps_sheet[0, 0].value, "Swap date")  # header present
        # Header + 1 data row = 2 visible rows (remaining ones are empty).
        self.assertEqual(swaps_sheet[1, 1].value, _ASSET)
        self.assertEqual(swaps_sheet[1, 6].value, "swap-X")

    def test_altvermoegen_within_1_year_routes_to_kz_801(self) -> None:
        # Buy 2020-06-01 (alt); sell 2020-12-01 (holding < 1 year) → Kz 801.
        computed = self._compute(
            in_txs=[self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[self._sell(row=2, timestamp="2020-12-01 00:00:00 +0000", crypto_out="0.5", spot_price="300", notes="at_regime=alt")],
        )
        path: Path = self._run_generator(computed)
        doc: Any = ezodf.opendoc(str(path))
        row_801: int = self._find_row_with_label(doc, "FinanzOnline", "801")
        # Gain = 0.5*(300-100) = 100.
        self.assertEqual(self._sheet_cell(doc, "FinanzOnline", row_801, 2), "100.00")
        # Alt disposal listed with TAXABLE status.
        alt_sheet: Any = doc.sheets["Altvermoegen Disposals"]
        # Header row + 1 data row.
        status_col: int = alt_sheet.ncols() - 1
        self.assertEqual(alt_sheet[1, status_col].value, "TAXABLE (Kz 801)")

    def test_altvermoegen_over_1_year_is_tax_free(self) -> None:
        # Buy 2019-01-01 (alt); sell 2023-06-01 → > 1 year → tax-free (not in Kz 801).
        computed = self._compute(
            in_txs=[self._buy(row=1, timestamp="2019-01-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[self._sell(row=2, timestamp="2023-06-01 00:00:00 +0000", crypto_out="0.5", spot_price="500", notes="at_regime=alt")],
        )
        path: Path = self._run_generator(computed)
        doc: Any = ezodf.opendoc(str(path))
        row_801: int = self._find_row_with_label(doc, "FinanzOnline", "801")
        self.assertEqual(self._sheet_cell(doc, "FinanzOnline", row_801, 2), "0.00")
        alt_sheet: Any = doc.sheets["Altvermoegen Disposals"]
        status_col: int = alt_sheet.ncols() - 1
        self.assertEqual(alt_sheet[1, status_col].value, "TAX-FREE")


if __name__ == "__main__":
    unittest.main()
