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
from datetime import date, datetime
from typing import Dict, List, Tuple, cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.out_transaction import OutTransaction
from rp2.per_wallet_tax_engine import compute_tax_per_wallet
from rp2.plugin.accounting_method.fifo import AccountingMethod as FifoAccountingMethod
from rp2.plugin.accounting_method.hifo import AccountingMethod as HifoAccountingMethod
from rp2.plugin.accounting_method.lofo import AccountingMethod as LofoAccountingMethod
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
            cost_basis_timestamp="2019-12-01 08:00:00 +0000",
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
        self.assertEqual(per_wallet_computed.get_crypto_in_running_sum(in_cb), RP2Decimal("2.0"))
        self.assertEqual(per_wallet_computed.get_crypto_in_running_sum(in_kr), RP2Decimal("3.0"))

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
        self.assertEqual(artificial_lots[0].cost_basis_timestamp, in_cb.cost_basis_timestamp)
        self.assertEqual(per_wallet_computed.get_crypto_in_running_sum(artificial_lots[0]), RP2Decimal("3.0"))

    def test_per_wallet_transfer_of_earn_lot_does_not_double_count_income(self) -> None:
        interest = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Interest",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="1",
            row=1,
        )
        intra = IntraTransaction(
            self._configuration,
            "2020-01-02 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Bob",
            spot_price=RP2Decimal("100"),
            crypto_sent=RP2Decimal("1.0"),
            crypto_received=RP2Decimal("1.0"),
            unique_id="2",
            row=2,
        )
        input_data = InputData("B1", self._build_in_set([interest]), self._build_out_set([]), self._build_intra_set([intra]))

        universal_computed = compute_tax(self._configuration, self._accounting_engine, input_data)
        per_wallet_computed = compute_tax_per_wallet(self._configuration, self._accounting_engine, self._transfer_semantics, input_data)

        universal_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_amount) for y in universal_computed.yearly_gain_loss_list]
        per_wallet_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_amount) for y in per_wallet_computed.yearly_gain_loss_list]
        self.assertEqual(universal_totals, per_wallet_totals)

        artificial_lots: List[InTransaction] = [
            cast(InTransaction, transaction) for transaction in per_wallet_computed.in_transaction_set if cast(InTransaction, transaction).from_lot is not None
        ]
        self.assertEqual(len(artificial_lots), 1)
        self.assertFalse(artificial_lots[0].is_taxable())

    def test_per_wallet_preserves_pre_window_acquisition_history(self) -> None:
        configuration = Configuration("./config/test_data.ini", US(), from_date=date(2021, 1, 1), to_date=date(2021, 12, 31))
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, FifoAccountingMethod())
        accounting_engine = AccountingEngine(years_2_methods)

        in_transaction = InTransaction(
            configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="1",
            row=1,
        )
        out_transaction = OutTransaction(
            configuration,
            "2021-02-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("200"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="2",
            row=2,
        )
        in_set = TransactionSet(configuration, "IN", "B1")
        in_set.add_entry(in_transaction)
        out_set = TransactionSet(configuration, "OUT", "B1")
        out_set.add_entry(out_transaction)
        intra_set = TransactionSet(configuration, "INTRA", "B1")
        input_data = InputData("B1", in_set, out_set, intra_set, from_date=configuration.from_date, to_date=configuration.to_date)

        universal_computed = compute_tax(configuration, accounting_engine, input_data)
        per_wallet_computed = compute_tax_per_wallet(configuration, accounting_engine, FifoAccountingMethod(), input_data)

        universal_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_gain_loss) for y in universal_computed.yearly_gain_loss_list]
        per_wallet_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_gain_loss) for y in per_wallet_computed.yearly_gain_loss_list]
        self.assertEqual(universal_totals, per_wallet_totals)

    def test_per_wallet_merged_yearly_totals_keep_same_year_pre_window_sales(self) -> None:
        configuration = Configuration("./config/test_data.ini", US(), from_date=date(2021, 9, 1), to_date=date(2021, 12, 31))
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, FifoAccountingMethod())
        accounting_engine = AccountingEngine(years_2_methods)

        buy_early = InTransaction(
            configuration,
            "2021-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="1",
            row=1,
        )
        sell_early = OutTransaction(
            configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("150"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="2",
            row=2,
        )
        buy_late = InTransaction(
            configuration,
            "2021-09-10 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("120"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="3",
            row=3,
        )
        sell_late = OutTransaction(
            configuration,
            "2021-09-15 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("200"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="4",
            row=4,
        )
        in_set = TransactionSet(configuration, "IN", "B1")
        in_set.add_entry(buy_early)
        in_set.add_entry(buy_late)
        out_set = TransactionSet(configuration, "OUT", "B1")
        out_set.add_entry(sell_early)
        out_set.add_entry(sell_late)
        intra_set = TransactionSet(configuration, "INTRA", "B1")
        input_data = InputData("B1", in_set, out_set, intra_set, from_date=configuration.from_date, to_date=configuration.to_date)

        universal_computed = compute_tax(configuration, accounting_engine, input_data)
        per_wallet_computed = compute_tax_per_wallet(configuration, accounting_engine, FifoAccountingMethod(), input_data)

        universal_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_gain_loss) for y in universal_computed.yearly_gain_loss_list]
        per_wallet_totals = [(y.year, y.transaction_type, y.crypto_amount, y.fiat_gain_loss) for y in per_wallet_computed.yearly_gain_loss_list]
        self.assertEqual(universal_totals, per_wallet_totals)

    def test_per_wallet_preserves_cost_basis_tie_break_for_feature_based_methods(self) -> None:
        source_lot = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="1",
            row=1,
        )
        local_lot = InTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="2",
            row=2,
        )
        transfer = IntraTransaction(
            self._configuration,
            "2020-12-01 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Bob",
            spot_price=RP2Decimal("100"),
            crypto_sent=RP2Decimal("1.0"),
            crypto_received=RP2Decimal("1.0"),
            unique_id="3",
            row=3,
        )
        sell = OutTransaction(
            self._configuration,
            "2021-01-10 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("200"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="4",
            row=4,
        )
        input_data = InputData(
            "B1",
            self._build_in_set([source_lot, local_lot]),
            self._build_out_set([sell]),
            self._build_intra_set([transfer]),
        )

        for transfer_semantics in [HifoAccountingMethod(), LofoAccountingMethod()]:
            with self.subTest(method=repr(transfer_semantics)):
                years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
                years_2_methods.insert_node(MIN_DATE.year, transfer_semantics)
                accounting_engine = AccountingEngine(years_2_methods)

                universal_computed = compute_tax(self._configuration, accounting_engine, input_data)
                per_wallet_computed = compute_tax_per_wallet(self._configuration, accounting_engine, transfer_semantics, input_data)

                universal_gain_loss = cast(GainLoss, next(iter(universal_computed.gain_loss_set)))
                per_wallet_gain_loss = cast(GainLoss, next(iter(per_wallet_computed.gain_loss_set)))
                self.assertEqual(universal_gain_loss.is_long_term_capital_gains(), per_wallet_gain_loss.is_long_term_capital_gains())
                self.assertTrue(per_wallet_gain_loss.is_long_term_capital_gains())

    def test_per_wallet_preserves_partial_transferred_lot_for_feature_based_methods(self) -> None:
        source_lot = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("2.0"),
            unique_id="1",
            row=1,
        )
        local_lot = InTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("100"),
            crypto_in=RP2Decimal("1.0"),
            unique_id="2",
            row=2,
        )
        transfer = IntraTransaction(
            self._configuration,
            "2020-12-01 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Bob",
            spot_price=RP2Decimal("100"),
            crypto_sent=RP2Decimal("2.0"),
            crypto_received=RP2Decimal("2.0"),
            unique_id="3",
            row=3,
        )
        first_sell = OutTransaction(
            self._configuration,
            "2021-01-10 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("200"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="4",
            row=4,
        )
        second_sell = OutTransaction(
            self._configuration,
            "2021-01-11 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("210"),
            crypto_out_no_fee=RP2Decimal("1.0"),
            crypto_fee=RP2Decimal("0"),
            unique_id="5",
            row=5,
        )
        input_data = InputData(
            "B1",
            self._build_in_set([source_lot, local_lot]),
            self._build_out_set([first_sell, second_sell]),
            self._build_intra_set([transfer]),
        )

        for transfer_semantics in [HifoAccountingMethod(), LofoAccountingMethod()]:
            with self.subTest(method=repr(transfer_semantics)):
                years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
                years_2_methods.insert_node(MIN_DATE.year, transfer_semantics)
                accounting_engine = AccountingEngine(years_2_methods)

                universal_computed = compute_tax(self._configuration, accounting_engine, input_data)
                per_wallet_computed = compute_tax_per_wallet(self._configuration, accounting_engine, transfer_semantics, input_data)

                universal_gain_losses = [cast(GainLoss, gain_loss) for gain_loss in universal_computed.gain_loss_set]
                per_wallet_gain_losses = [cast(GainLoss, gain_loss) for gain_loss in per_wallet_computed.gain_loss_set]

                expected_long_term: List[bool] = [True, True]
                universal_long_term: List[bool] = [gain_loss.is_long_term_capital_gains() for gain_loss in universal_gain_losses]
                per_wallet_long_term: List[bool] = [gain_loss.is_long_term_capital_gains() for gain_loss in per_wallet_gain_losses]
                expected_cost_basis_timestamps: List[datetime] = [source_lot.cost_basis_timestamp, source_lot.cost_basis_timestamp]
                universal_cost_basis_timestamps: List[datetime] = [
                    cast(InTransaction, gain_loss.acquired_lot).cost_basis_timestamp for gain_loss in universal_gain_losses
                ]
                per_wallet_cost_basis_timestamps: List[datetime] = [
                    cast(InTransaction, gain_loss.acquired_lot).cost_basis_timestamp for gain_loss in per_wallet_gain_losses
                ]
                self.assertEqual(universal_long_term, expected_long_term)
                self.assertEqual(per_wallet_long_term, universal_long_term)
                self.assertEqual(universal_cost_basis_timestamps, expected_cost_basis_timestamps)
                self.assertEqual(per_wallet_cost_basis_timestamps, universal_cost_basis_timestamps)


if __name__ == "__main__":
    unittest.main()
