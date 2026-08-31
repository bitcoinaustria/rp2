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

# pylint: disable=too-many-lines

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
from rp2.in_transaction import Account, InTransaction
from rp2.input_data import InputData
from rp2.intra_transaction import IntraTransaction
from rp2.out_transaction import OutTransaction
from rp2.per_wallet_tax_engine import compute_tax_per_wallet
from rp2.plugin.accounting_method.fifo import AccountingMethod as FifoAccountingMethod
from rp2.plugin.accounting_method.hifo import AccountingMethod as HifoAccountingMethod
from rp2.plugin.accounting_method.lofo import AccountingMethod as LofoAccountingMethod
from rp2.plugin.accounting_method.moving_average import (
    AccountingMethod as MovingAverageAccountingMethod,
)
from rp2.plugin.country.us import US
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2ValueError
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet
from rp2.transfer_analyzer import TransferAnalyzer


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

    def test_per_wallet_transfer_into_wallet_with_same_timestamp_lot(self) -> None:
        # Regression: the artificial "to" lot created for a transfer carries a negative internal id
        # and its timestamp is the transfer timestamp. When the destination wallet already owns a
        # real lot at the EXACT same timestamp, the AVL disambiguator must sort the artificial lot
        # above the real one (matching its position in the chronological lot list). The old raw-id
        # padding sorted '-' below '0', so the artificial lot fell out of the candidate window and
        # the destination sell failed with "Total in-transaction crypto value < total taxable".
        transfer_timestamp = "2020-06-01 08:00:00 +0000"
        in_cb = InTransaction(
            self._configuration,
            "2020-01-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("10000"),
            crypto_in=RP2Decimal("1.0"),
            fiat_in_no_fee=RP2Decimal("10000"),
            fiat_in_with_fee=RP2Decimal("10000"),
            fiat_fee=RP2Decimal("0"),
            unique_id="1",
            row=1,
        )
        in_kr_same_ts = InTransaction(
            self._configuration,
            transfer_timestamp,  # same instant as the transfer below
            "B1",
            "Kraken",
            "Alice",
            "Buy",
            spot_price=RP2Decimal("20000"),
            crypto_in=RP2Decimal("1.0"),
            fiat_in_no_fee=RP2Decimal("20000"),
            fiat_in_with_fee=RP2Decimal("20000"),
            fiat_fee=RP2Decimal("0"),
            unique_id="2",
            row=2,
        )
        intra = IntraTransaction(
            self._configuration,
            transfer_timestamp,
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
            crypto_out_no_fee=RP2Decimal("2.0"),  # consumes the real Kraken lot AND the transferred lot
            crypto_fee=RP2Decimal("0"),
            unique_id="4",
            row=4,
        )

        input_data = InputData(
            "B1",
            self._build_in_set([in_cb, in_kr_same_ts]),
            self._build_out_set([out_kr]),
            self._build_intra_set([intra]),
        )

        # Must not raise: the transferred lot is found despite sharing a timestamp with the real lot.
        per_wallet_computed = compute_tax_per_wallet(self._configuration, self._accounting_engine, self._transfer_semantics, input_data)
        disposed = sum((g.crypto_amount for g in per_wallet_computed.gain_loss_set if isinstance(g, GainLoss)), RP2Decimal("0"))
        self.assertEqual(disposed, RP2Decimal("2.0"))

    def test_per_wallet_transfer_carries_moving_average_basis(self) -> None:
        input_data, _intra = self._moving_average_transfer_fixture(crypto_sent="1.5", crypto_received="1.5")
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)

        computed = compute_tax_per_wallet(self._configuration, AccountingEngine(years_2_methods), method, input_data)

        gain_losses: List[GainLoss] = [cast(GainLoss, entry) for entry in computed.gain_loss_set]
        sale_gain_losses = [gain_loss for gain_loss in gain_losses if isinstance(gain_loss.taxable_event, OutTransaction)]
        artificial_lots = [
            cast(InTransaction, transaction) for transaction in computed.in_transaction_set if cast(InTransaction, transaction).from_lot is not None
        ]
        self.assertEqual(len(artificial_lots), 2)
        self.assertEqual(sum((gain_loss.fiat_cost_basis for gain_loss in sale_gain_losses), RP2Decimal("0")), RP2Decimal("300"))
        self.assertEqual(sum((gain_loss.fiat_gain for gain_loss in sale_gain_losses), RP2Decimal("0")), RP2Decimal("450"))
        self.assertEqual(sum((lot.fiat_in_with_fee for lot in artificial_lots), RP2Decimal("0")), RP2Decimal("300"))
        self.assertTrue(all(lot.fiat_fee == RP2Decimal("0") for lot in artificial_lots))

    def test_moving_average_multi_lot_transfer_uses_remaining_amount(self) -> None:
        source_low, source_high = self._moving_average_source_lots()
        source_later = InTransaction(
            self._configuration,
            "2020-07-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("500"),
            crypto_in=RP2Decimal("1"),
            unique_id="ma-later",
            row=5,
        )
        first_transfer = self._moving_average_intra("ma-first", "2020-06-01 08:00:00 +0000", "Coinbase", "Kraken", "1.5", "1.5")
        second_transfer = self._moving_average_intra("ma-second", "2020-08-01 08:00:00 +0000", "Coinbase", "BlockFi", "0.5", "0.5")
        blockfi_sale = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "BlockFi",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("1000"),
            crypto_out_no_fee=RP2Decimal("0.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="ma-blockfi-sale",
            row=6,
        )
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)

        computed = compute_tax_per_wallet(
            self._configuration,
            AccountingEngine(years_2_methods),
            method,
            InputData(
                "B1",
                self._build_in_set([source_low, source_high, source_later]),
                self._build_out_set([blockfi_sale]),
                self._build_intra_set([first_transfer, second_transfer]),
            ),
        )

        blockfi_lot = next(
            cast(InTransaction, transaction)
            for transaction in computed.in_transaction_set
            if cast(InTransaction, transaction).unique_id.startswith("ma-second/")
        )
        blockfi_gain_loss = next(cast(GainLoss, entry) for entry in computed.gain_loss_set if cast(GainLoss, entry).taxable_event is blockfi_sale)
        self.assertEqual(blockfi_lot.fiat_in_with_fee, RP2Decimal("200"))
        self.assertEqual(blockfi_gain_loss.fiat_cost_basis, RP2Decimal("200"))

    def test_moving_average_multi_lot_sale_uses_remaining_amount(self) -> None:
        source_low, source_high = self._moving_average_source_lots()
        source_sale = OutTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("400"),
            crypto_out_no_fee=RP2Decimal("1.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="ma-source-sale",
            row=5,
        )
        source_later = InTransaction(
            self._configuration,
            "2020-07-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Buy",
            spot_price=RP2Decimal("500"),
            crypto_in=RP2Decimal("1"),
            unique_id="ma-later",
            row=6,
        )
        transfer = self._moving_average_intra("ma-after-sale", "2020-08-01 08:00:00 +0000", "Coinbase", "BlockFi", "0.5", "0.5")
        blockfi_sale = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "BlockFi",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("1000"),
            crypto_out_no_fee=RP2Decimal("0.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="ma-blockfi-sale",
            row=7,
        )
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)

        computed = compute_tax_per_wallet(
            self._configuration,
            AccountingEngine(years_2_methods),
            method,
            InputData(
                "B1",
                self._build_in_set([source_low, source_high, source_later]),
                self._build_out_set([source_sale, blockfi_sale]),
                self._build_intra_set([transfer]),
            ),
        )

        blockfi_lot = next(
            cast(InTransaction, transaction)
            for transaction in computed.in_transaction_set
            if cast(InTransaction, transaction).unique_id.startswith("ma-after-sale/")
        )
        blockfi_gain_loss = next(cast(GainLoss, entry) for entry in computed.gain_loss_set if cast(GainLoss, entry).taxable_event is blockfi_sale)
        self.assertEqual(blockfi_lot.fiat_in_with_fee, RP2Decimal("200"))
        self.assertEqual(blockfi_gain_loss.fiat_cost_basis, RP2Decimal("200"))

    def test_per_wallet_merge_preserves_tax_engine_effective_basis(self) -> None:
        source_low, source_high = self._moving_average_source_lots()
        source_sale = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("500"),
            crypto_out_no_fee=RP2Decimal("0.5"),
            crypto_fee=RP2Decimal("0"),
            unique_id="ma-source-sale",
            row=5,
        )
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)

        computed = compute_tax_per_wallet(
            self._configuration,
            AccountingEngine(years_2_methods),
            FifoAccountingMethod(),
            InputData(
                "B1",
                self._build_in_set([source_low, source_high]),
                self._build_out_set([source_sale]),
                self._build_intra_set([]),
            ),
        )

        self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(source_low), RP2Decimal("200"))
        self.assertEqual(computed.get_in_transaction_fiat_in_with_fee(source_high), RP2Decimal("200"))

    def test_per_wallet_rejects_fee_bearing_cross_account_transfer(self) -> None:
        input_data, _intra = self._moving_average_transfer_fixture(crypto_sent="1.1", crypto_received="1")

        for method in (FifoAccountingMethod(), MovingAverageAccountingMethod()):
            with self.subTest(method=method.name):
                years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
                years_2_methods.insert_node(MIN_DATE.year, method)
                with self.assertRaisesRegex(RP2ValueError, "Fee-bearing intra-transactions are unsupported with per_wallet"):
                    compute_tax_per_wallet(self._configuration, AccountingEngine(years_2_methods), method, input_data)

    def test_moving_average_pool_state_survives_full_self_transfer(self) -> None:
        self_transfer = self._moving_average_intra("ma-self", "2020-06-01 08:00:00 +0000", "Coinbase", "Coinbase", "2", "2")
        external_transfer = self._moving_average_intra("ma-external", "2020-07-01 08:00:00 +0000", "Coinbase", "Kraken", "1", "1")

        computed = self._compute_moving_average_fixture([self_transfer, external_transfer])

        destination_lot = next(
            cast(InTransaction, transaction)
            for transaction in computed.in_transaction_set
            if cast(InTransaction, transaction).unique_id.startswith("ma-external/")
        )
        self.assertEqual(destination_lot.fiat_in_with_fee, RP2Decimal("200"))

    def test_all_fee_bearing_transfers_fail_closed_in_both_orderings(self) -> None:
        for method in (FifoAccountingMethod(), MovingAverageAccountingMethod()):
            for self_transfer_has_fee in (False, True):
                for fee_transfer_first in (False, True):
                    with self.subTest(method=method.name, self_transfer_has_fee=self_transfer_has_fee, fee_transfer_first=fee_transfer_first):
                        fee_destination = "Coinbase" if self_transfer_has_fee else "Kraken"
                        fee_transfer = self._moving_average_intra(
                            f"fee-{method.name}-{self_transfer_has_fee}-{fee_transfer_first}",
                            "2020-06-01 08:00:00 +0000" if fee_transfer_first else "2020-07-01 08:00:00 +0000",
                            "Coinbase",
                            fee_destination,
                            "0.6",
                            "0.5",
                        )
                        fee_free_transfer = self._moving_average_intra(
                            f"free-{method.name}-{self_transfer_has_fee}-{fee_transfer_first}",
                            "2020-07-01 08:00:00 +0000" if fee_transfer_first else "2020-06-01 08:00:00 +0000",
                            "Coinbase",
                            "Kraken",
                            "1",
                            "1",
                        )
                        source_low, source_high = self._moving_average_source_lots()
                        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
                        years_2_methods.insert_node(MIN_DATE.year, method)
                        with self.assertRaisesRegex(RP2ValueError, "Fee-bearing intra-transactions are unsupported with per_wallet"):
                            compute_tax_per_wallet(
                                self._configuration,
                                AccountingEngine(years_2_methods),
                                method,
                                InputData(
                                    "B1",
                                    self._build_in_set([source_low, source_high]),
                                    self._build_out_set([]),
                                    self._build_intra_set([fee_transfer, fee_free_transfer]),
                                ),
                            )

    def test_moving_average_pool_state_survives_transfer_cycle(self) -> None:
        to_kraken = self._moving_average_intra("ma-cycle-1", "2020-06-01 08:00:00 +0000", "Coinbase", "Kraken", "2", "2")
        back_to_coinbase = self._moving_average_intra("ma-cycle-2", "2020-07-01 08:00:00 +0000", "Kraken", "Coinbase", "2", "2")
        to_blockfi = self._moving_average_intra("ma-cycle-3", "2020-08-01 08:00:00 +0000", "Coinbase", "BlockFi", "1", "1")

        source_low, source_high = self._moving_average_source_lots()
        method = MovingAverageAccountingMethod()
        wallet_inputs = TransferAnalyzer(
            self._configuration,
            method,
            InputData(
                "B1",
                self._build_in_set([source_low, source_high]),
                self._build_out_set([]),
                self._build_intra_set([to_kraken, back_to_coinbase, to_blockfi]),
            ),
        ).analyze()

        destination_lot = next(
            cast(InTransaction, transaction)
            for transaction in wallet_inputs[Account("BlockFi", "Bob")].unfiltered_in_transaction_set
            if cast(InTransaction, transaction).unique_id.startswith("ma-cycle-3/")
        )
        self.assertEqual(destination_lot.fiat_in_with_fee, RP2Decimal("200"))

    def test_fake_country_computation_hook_carries_basis_between_opaque_pools(self) -> None:
        input_data, _intra = self._moving_average_transfer_fixture(crypto_sent="1", crypto_received="1")
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)

        class FakePoolCountry(US):
            def compute_tax_for_assets(
                self,
                configuration: Configuration,
                accounting_engine: AccountingEngine,
                asset_to_input_data: Dict[str, InputData],
            ) -> Dict[str, ComputedData]:
                return {
                    asset: compute_tax_per_wallet(configuration, accounting_engine, method, asset_input) for asset, asset_input in asset_to_input_data.items()
                }

        result = FakePoolCountry().compute_tax_for_assets(
            self._configuration,
            AccountingEngine(years_2_methods),
            {"B1": input_data},
        )

        gain_losses = [cast(GainLoss, entry) for entry in result["B1"].gain_loss_set]
        self.assertEqual(len(gain_losses), 1)
        self.assertEqual(gain_losses[0].fiat_cost_basis, RP2Decimal("200"))
        open_basis = sum(
            (
                result["B1"].get_in_transaction_fiat_in_with_fee(cast(InTransaction, entry))
                * cast(RP2Decimal, result["B1"].get_in_transaction_actual_amount(cast(InTransaction, entry)))
                / cast(InTransaction, entry).crypto_in
                for entry in result["B1"].open_position_in_transaction_set
                if result["B1"].get_in_transaction_actual_amount(cast(InTransaction, entry)) is not None
            ),
            RP2Decimal("0"),
        )
        self.assertEqual(open_basis, RP2Decimal("200"))
        self.assertEqual(open_basis + gain_losses[0].fiat_cost_basis, RP2Decimal("400"))

    def _compute_moving_average_fixture(self, intras: List[IntraTransaction]) -> ComputedData:
        source_low, source_high = self._moving_average_source_lots()
        method = MovingAverageAccountingMethod()
        years_2_methods: AVLTree[int, AbstractAccountingMethod] = AVLTree()
        years_2_methods.insert_node(MIN_DATE.year, method)
        return compute_tax_per_wallet(
            self._configuration,
            AccountingEngine(years_2_methods),
            method,
            InputData(
                "B1",
                self._build_in_set([source_low, source_high]),
                self._build_out_set([]),
                self._build_intra_set(intras),
            ),
        )

    def _moving_average_source_lots(self, source_low_amount: str = "1") -> Tuple[InTransaction, InTransaction]:
        return (
            InTransaction(
                self._configuration,
                "2020-01-01 08:00:00 +0000",
                "B1",
                "Coinbase",
                "Bob",
                "Buy",
                spot_price=RP2Decimal("100"),
                crypto_in=RP2Decimal(source_low_amount),
                unique_id="ma-1",
                row=1,
            ),
            InTransaction(
                self._configuration,
                "2020-01-02 08:00:00 +0000",
                "B1",
                "Coinbase",
                "Bob",
                "Buy",
                spot_price=RP2Decimal("300"),
                crypto_in=RP2Decimal("1"),
                unique_id="ma-2",
                row=2,
            ),
        )

    def _moving_average_intra(
        self,
        unique_id: str,
        timestamp: str,
        from_exchange: str,
        to_exchange: str,
        crypto_sent: str,
        crypto_received: str,
    ) -> IntraTransaction:
        return IntraTransaction(
            self._configuration,
            timestamp,
            "B1",
            from_exchange=from_exchange,
            from_holder="Bob",
            to_exchange=to_exchange,
            to_holder="Bob",
            spot_price=RP2Decimal("500"),
            crypto_sent=RP2Decimal(crypto_sent),
            crypto_received=RP2Decimal(crypto_received),
            unique_id=unique_id,
            row=100 + int(timestamp[5:7]),
        )

    def _moving_average_transfer_fixture(
        self,
        crypto_sent: str,
        crypto_received: str,
        source_low_amount: str = "1",
    ) -> Tuple[InputData, IntraTransaction]:
        source_low, source_high = self._moving_average_source_lots(source_low_amount)
        intra = IntraTransaction(
            self._configuration,
            "2020-06-01 08:00:00 +0000",
            "B1",
            from_exchange="Coinbase",
            from_holder="Bob",
            to_exchange="Kraken",
            to_holder="Bob",
            spot_price=RP2Decimal("500"),
            crypto_sent=RP2Decimal(crypto_sent),
            crypto_received=RP2Decimal(crypto_received),
            unique_id="ma-3",
            row=3,
        )
        sale = OutTransaction(
            self._configuration,
            "2021-03-01 08:00:00 +0000",
            "B1",
            "Kraken",
            "Bob",
            "Sell",
            spot_price=RP2Decimal("500"),
            crypto_out_no_fee=RP2Decimal(crypto_received),
            crypto_fee=RP2Decimal("0"),
            unique_id="ma-4",
            row=4,
        )
        return (
            InputData(
                "B1",
                self._build_in_set([source_low, source_high]),
                self._build_out_set([sale]),
                self._build_intra_set([intra]),
            ),
            intra,
        )

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
