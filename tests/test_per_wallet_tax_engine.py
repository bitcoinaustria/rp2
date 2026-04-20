# Copyright 2025 eprbell
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

# Smoke tests for the per-wallet tax engine integration.
#
# The test fixture is synthesized so it is valid under both universal and per-wallet
# application (no under-balanced wallets). Two scenarios:
#
# 1) single wallet, no intras: per-wallet totals must match universal totals exactly.
# 2) two wallets with a transfer: per-wallet must run end-to-end and the merged
#    ComputedData must preserve every original out/intra transaction.

import unittest
from typing import Dict, List, Tuple, cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.out_transaction import OutTransaction
from rp2.per_wallet_tax_engine import compute_tax_per_wallet
from rp2.plugin.accounting_method.fifo import AccountingMethod as FifoAccountingMethod
from rp2.plugin.country.us import US
from rp2.rp2_decimal import RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet


class TestPerWalletTaxEngine(unittest.TestCase):
    _configuration: Configuration
    _accounting_engine: AccountingEngine
    _transfer_semantics: AbstractAccountingMethod

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", US())
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, FifoAccountingMethod())
        cls._accounting_engine = AccountingEngine(years_2_methods)
        cls._transfer_semantics = FifoAccountingMethod()

    def setUp(self) -> None:
        self.maxDiff = None  # pylint: disable=invalid-name

    def _build_in_set(self, entries: "list[InTransaction]") -> TransactionSet:
        result = TransactionSet(self._configuration, "IN", "B1")
        for e in entries:
            result.add_entry(e)
        return result

    def _build_out_set(self, entries: "list[OutTransaction]") -> TransactionSet:
        result = TransactionSet(self._configuration, "OUT", "B1")
        for e in entries:
            result.add_entry(e)
        return result

    def _build_intra_set(self, entries: "list[IntraTransaction]") -> TransactionSet:
        result = TransactionSet(self._configuration, "INTRA", "B1")
        for e in entries:
            result.add_entry(e)
        return result

    def test_per_wallet_matches_universal_without_transfers(self) -> None:
        # One wallet, two buys, one sell. Per-wallet == universal in this shape.
        in1 = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("10000"),
            crypto_in=RP2Decimal("1.0"),
            fiat_in_no_fee=RP2Decimal("10000"),
            fiat_in_with_fee=RP2Decimal("10050"),
            fiat_fee=RP2Decimal("50"),
            unique_id="1",
            row=1,
        )
        in2 = InTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("20000"),
            crypto_in=RP2Decimal("1.0"),
            fiat_in_no_fee=RP2Decimal("20000"),
            fiat_in_with_fee=RP2Decimal("20100"),
            fiat_fee=RP2Decimal("100"),
            unique_id="2",
            row=2,
        )
        out1 = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("30000"),
            crypto_out_no_fee=RP2Decimal("0.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="3",
            row=3,
        )

        input_data = InputData("B1", self._build_in_set([in1, in2]), self._build_out_set([out1]), self._build_intra_set([]))

        universal_computed = compute_tax(self._configuration, self._accounting_engine, input_data)
        per_wallet_computed = compute_tax_per_wallet(self._configuration, self._accounting_engine, self._transfer_semantics, input_data)

        universal_totals = {
            (y.year, y.transaction_type, y.is_long_term_capital_gains): (y.crypto_amount, y.fiat_gain_loss) for y in universal_computed.yearly_gain_loss_list
        }
        per_wallet_totals = {
            (y.year, y.transaction_type, y.is_long_term_capital_gains): (y.crypto_amount, y.fiat_gain_loss) for y in per_wallet_computed.yearly_gain_loss_list
        }
        self.assertEqual(universal_totals, per_wallet_totals)
        self.assertGreater(len(universal_totals), 0, "expected at least one yearly gain/loss entry")

    def test_per_wallet_runs_end_to_end_with_transfers(self) -> None:
        # Two wallets, a transfer from Coinbase to Kraken, then a sell on Kraken.
        in_cb = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("10000"),
            crypto_in=RP2Decimal("2.0"),
            fiat_in_no_fee=RP2Decimal("20000"),
            fiat_in_with_fee=RP2Decimal("20100"),
            fiat_fee=RP2Decimal("100"),
            unique_id="1",
            row=1,
        )
        in_kr = InTransaction(
            self._configuration,
            "2020-01-02 08:00:00 +0000",
            "B1",
            "Kraken",
            "Alice",
            "Buy",
            spot_price=RP2Decimal("10000"),
            crypto_in=RP2Decimal("1.0"),
            fiat_in_no_fee=RP2Decimal("10000"),
            fiat_in_with_fee=RP2Decimal("10050"),
            fiat_fee=RP2Decimal("50"),
            unique_id="2",
            row=2,
        )
        intra = IntraTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Alice",
            spot_price=RP2Decimal("15000"),
            crypto_sent=RP2Decimal("1.0"),
            crypto_received=RP2Decimal("1.0"),
            unique_id="3",
            row=3,
        )
        out_kr = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "Kraken",
            "Alice",
            "Sell",
            spot_price=RP2Decimal("30000"),
            crypto_out_no_fee=RP2Decimal("1.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="4",
            row=4,
        )

        input_data = InputData(
            "B1",
            self._build_in_set([in_cb, in_kr]),
            self._build_out_set([out_kr]),
            self._build_intra_set([intra]),
        )

        per_wallet_computed: ComputedData = compute_tax_per_wallet(self._configuration, self._accounting_engine, self._transfer_semantics, input_data)

        self.assertEqual(per_wallet_computed.asset, "B1")
        # Per-wallet adds an artificial "to" InTransaction for the transfer.
        self.assertGreater(per_wallet_computed.in_transaction_set.count, input_data.filtered_in_transaction_set.count)
        self.assertEqual(per_wallet_computed.out_transaction_set.count, 1)
        self.assertEqual(per_wallet_computed.intra_transaction_set.count, 1)
        self.assertGreater(len(per_wallet_computed.yearly_gain_loss_list), 0)
        self.assertEqual(per_wallet_computed.yearly_gain_loss_list[0].fiat_cost_basis, RP2Decimal("15075.0"))
        self.assertEqual(per_wallet_computed.price_per_unit, RP2Decimal("10050"))

        balances: Dict[Tuple[str, str], RP2Decimal] = {
            (account.exchange, account.holder): balance.final_balance for account, balance in per_wallet_computed.balance_set.account_to_balance.items()
        }
        expected_balances: Dict[Tuple[str, str], RP2Decimal] = {
            ("Coinbase", "Bob"): RP2Decimal("1.0"),
            ("Kraken", "Alice"): RP2Decimal("0.5"),
        }
        self.assertEqual(balances, expected_balances)

        artificial_lots: List[InTransaction] = [
            cast(InTransaction, transaction) for transaction in per_wallet_computed.in_transaction_set if cast(InTransaction, transaction).from_lot is not None
        ]
        self.assertEqual(len(artificial_lots), 1)
        self.assertEqual(artificial_lots[0].fiat_in_no_fee, RP2Decimal("10000"))
        self.assertEqual(artificial_lots[0].fiat_fee, RP2Decimal("50"))
        self.assertEqual(artificial_lots[0].fiat_in_with_fee, RP2Decimal("10050"))


if __name__ == "__main__":
    unittest.main()
