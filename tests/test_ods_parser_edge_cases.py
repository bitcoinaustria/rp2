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

# mypy: disallow-any-expr=False, disallow-any-explicit=False

import unittest
from typing import Any, cast

import ezodf
from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.configuration import Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.ods_parser import open_ods, parse_ods
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.moving_average_at import AccountingMethod
from rp2.plugin.country.at import (
    AT,
    REGIME_NEU,
    classify_lot_regime,
    pool_id_from_notes,
    swap_link_id,
)
from rp2.plugin.country.us import US
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2RuntimeError, RP2ValueError
from rp2.tax_engine import compute_tax


class TestODSParserEdgeCases(unittest.TestCase):
    def test_fee_split_preserves_austrian_acquisition_markers(self) -> None:
        for notes in ("at_regime=neu", "at_pool=savings", "at_swap_link=swap-1"):
            with self.subTest(notes=notes):
                configuration = Configuration("./config/test_data4.ini", AT())
                document = open_ods(configuration, "./input/test_data4.ods")
                document.sheets["B1"][2, 10].set_value(notes)
                data = parse_ods(configuration, "B1", document)
                acquisition = cast(InTransaction, next(iter(data.unfiltered_in_transaction_set)))
                if notes.startswith("at_regime="):
                    self.assertEqual(classify_lot_regime(acquisition), REGIME_NEU)
                elif notes.startswith("at_pool="):
                    self.assertEqual(pool_id_from_notes(acquisition.notes), "savings")
                else:
                    self.assertEqual(swap_link_id(acquisition), "swap-1")
                fee = next(event for event in data.unfiltered_out_transaction_set if event.internal_id == "-1")
                self.assertIsNone(swap_link_id(cast(OutTransaction, fee)))

    def test_fee_split_charges_the_acquisition_pool(self) -> None:
        configuration = Configuration("./config/test_data4.ini", AT())
        document = open_ods(configuration, "./input/test_data4.ods")
        sheet = document.sheets["B1"]
        sheet[2, 10].set_value("at_pool=savings at_regime=neu")
        sheet[3, 10].set_value("at_pool=savings at_regime=neu")
        sheet[8, 10].set_value("at_pool=savings at_regime=neu")
        data = parse_ods(configuration, "B1", document)
        methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        methods.insert_node(1970, AccountingMethod())
        engine = AccountingEngine(methods)
        computed = compute_tax(configuration, engine, data)
        fees = [cast(GainLoss, row) for row in computed.gain_loss_set if cast(GainLoss, row).taxable_event.internal_id.startswith("-")]
        self.assertEqual(len(fees), 2)
        self.assertEqual(fees[0].fiat_cost_basis, RP2Decimal("1.1"))
        self.assertEqual(fees[1].fiat_cost_basis, RP2Decimal("6.1959183673469387755102040816327"))

    def test_native_swap_carries_basis_before_charging_incoming_crypto_fee(self) -> None:
        configuration = Configuration("./config/test_data4.ini", AT())
        document: Any = ezodf.newdoc(doctype="ods", filename="")
        for asset in ("B1", "B2"):
            document.sheets += ezodf.Sheet(asset, size=(9, 11))
        source_rows: list[list[object]] = [
            ["IN"],
            ["Timestamp"],
            ["2023-01-01T00:00Z", "Coinbase", "Bob", None, None, "Buy", "B1", 1.0, 100.0, 0.0],
            ["TABLE END"],
            [],
            ["OUT"],
            ["Timestamp"],
            ["2023-02-01T00:00Z", "Coinbase", "Bob", None, None, "Sell", "B1", 1.0, 1000.0, 0.0, "at_swap_link=fee-swap"],
            ["TABLE END"],
        ]
        destination_rows: list[list[object]] = [
            ["IN"],
            ["Timestamp"],
            ["2023-02-01T00:00Z", "Coinbase", "Bob", None, None, "Buy", "B2", 2.0, 500.0, 0.2, "at_pool=savings at_swap_link=fee-swap"],
            ["TABLE END"],
        ]
        for asset, rows in (("B1", source_rows), ("B2", destination_rows)):
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    if value is not None:
                        document.sheets[asset][row_index, column_index].set_value(value)
        inputs = {asset: parse_ods(configuration, asset, document) for asset in ("B1", "B2")}
        methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        methods.insert_node(1970, AccountingMethod())
        computed = AT().compute_tax_for_assets(configuration, AccountingEngine(methods), inputs)
        self.assertIsNotNone(computed)
        if computed is None:
            self.fail("Expected native swap computation")
        acquisition = cast(InTransaction, next(iter(inputs["B2"].unfiltered_in_transaction_set)))
        self.assertEqual(computed["B2"].get_in_transaction_fiat_in_with_fee(acquisition), RP2Decimal("100"))
        fee_rows = [cast(GainLoss, row) for row in computed["B2"].gain_loss_set]
        self.assertEqual(len(fee_rows), 1)
        self.assertEqual(fee_rows[0].fiat_cost_basis, RP2Decimal("10"))
        self.assertIsNone(swap_link_id(fee_rows[0].taxable_event))

    def test_small_crypto_amount_keeps_supported_decimal_precision(self) -> None:
        configuration = Configuration("./config/test_data4.ini", US())
        document = open_ods(configuration, "./input/test_data4.ods")
        document.sheets["B1"][2, 7].set_value(0.0000000000012)
        document.sheets["B1"][2, 9].set_value(0.0)
        data = parse_ods(configuration, "B1", document)
        acquisition = cast(InTransaction, next(iter(data.unfiltered_in_transaction_set)))
        self.assertEqual(acquisition.crypto_in, RP2Decimal("0.0000000000012"))

    def test_unresolved_dali_amount_retains_actionable_error(self) -> None:
        configuration = Configuration("./config/test_data4.ini", US())
        document = open_ods(configuration, "./input/test_data4.ods")
        document.sheets["B1"][2, 7].set_value("__unknown")
        with self.assertRaisesRegex(RP2RuntimeError, r"B1\(3\).*unresolved DaLI transaction"):
            parse_ods(configuration, "B1", document)

    def test_numeric_cell_type_error_includes_asset_row_and_field(self) -> None:
        configuration = Configuration("./config/test_data4.ini", US())
        document = open_ods(configuration, "./input/test_data4.ods")
        document.sheets["B1"][2, 7].set_value(True)
        with self.assertRaisesRegex(RP2ValueError, r"B1\(3\).*crypto_in.*non-numeric"):
            parse_ods(configuration, "B1", document)


if __name__ == "__main__":
    unittest.main()
