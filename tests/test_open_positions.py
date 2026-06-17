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
from datetime import date
from typing import cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.balance import CRYPTO_BALANCE_DECIMAL_MASK
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.ods_parser import open_ods, parse_ods
from rp2.plugin.accounting_method.fifo import AccountingMethod
from rp2.plugin.country.us import US
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet


class TestOpenPositions(unittest.TestCase):
    _accounting_engine: AccountingEngine

    @classmethod
    def setUpClass(cls) -> None:
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree[int, AbstractAccountingMethod]()
        years_2_methods.insert_node(MIN_DATE.year, AccountingMethod())
        cls._accounting_engine = AccountingEngine(years_2_methods)

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name

    def _computed_data(self, asset: str, from_date: date) -> ComputedData:
        config: Configuration = Configuration("./config/test_data.ini", US(), from_date=from_date)
        input_file_handle: object = open_ods(config, "./input/test_data.ods")
        input_data: InputData = parse_ods(config, asset, input_file_handle)
        return compute_tax(config, self._accounting_engine, input_data)

    @staticmethod
    def _iter_count(transaction_set: TransactionSet) -> int:
        # TransactionSet.count returns the unfiltered total; iteration honors the from_date/to_date window.
        return sum(1 for _ in transaction_set)

    @staticmethod
    def _open_position_cost_basis(computed_data: ComputedData) -> RP2Decimal:
        # Mirrors the universal-application cost-basis aggregation in the open_positions report.
        total: RP2Decimal = ZERO
        for entry in computed_data.open_position_in_transaction_set:
            in_transaction: InTransaction = cast(InTransaction, entry)
            effective_fiat_in_with_fee: RP2Decimal = computed_data.get_in_transaction_fiat_in_with_fee(in_transaction)
            sold_percent: RP2Decimal = computed_data.get_open_position_in_lot_sold_percentage(in_transaction)
            cost_basis: RP2Decimal = effective_fiat_in_with_fee * (RP2Decimal("1") - sold_percent)
            if cost_basis > ZERO:
                total += cost_basis
        return total

    def test_open_positions_are_independent_of_from_date(self) -> None:
        # Asset B1 in input/test_data.ods has five in-lots spanning 2020-01 .. 2020-05 and no disposals, so all
        # lots remain open. Restricting -f/--from-date to 2020-03-01 must NOT change the open-position view (the
        # holdings as of to_date are the same), even though the from_date-filtered set used by period reports
        # shrinks. See https://github.com/bitcoinaustria/rp2/issues/8 (mirrors upstream eprbell/rp2#105).
        full: ComputedData = self._computed_data("B1", MIN_DATE)
        windowed: ComputedData = self._computed_data("B1", date(2020, 3, 1))

        # The from_date window genuinely drops early lots from the period-scoped (filtered) view ...
        self.assertLess(self._iter_count(windowed.in_transaction_set), self._iter_count(full.in_transaction_set))
        # ... but the open-position view iterates the same lots regardless of from_date ...
        self.assertEqual(
            self._iter_count(windowed.open_position_in_transaction_set),
            self._iter_count(full.open_position_in_transaction_set),
        )
        self.assertEqual(self._iter_count(full.open_position_in_transaction_set), self._iter_count(full.in_transaction_set))
        # ... and so is the open-position cost basis (the bug: it used to shrink with from_date).
        self.assertEqual(self._open_position_cost_basis(windowed), self._open_position_cost_basis(full))
        self.assertGreater(self._open_position_cost_basis(full), ZERO)

    def test_dust_residual_is_treated_as_zero_balance(self) -> None:
        # open_positions ignores sub-1e-10 residual balances (issue #9, mirrors upstream eprbell/rp2#112), using
        # the same precision mask the balance engine applies to its zero checks. Guard the threshold semantics:
        # genuine dust collapses to zero while a real (if tiny) holding does not.
        self.assertTrue(RP2Decimal.is_equal_within_precision(RP2Decimal("0.00000000003"), ZERO, CRYPTO_BALANCE_DECIMAL_MASK))
        self.assertFalse(RP2Decimal.is_equal_within_precision(RP2Decimal("0.00000001"), ZERO, CRYPTO_BALANCE_DECIMAL_MASK))


if __name__ == "__main__":
    unittest.main()
