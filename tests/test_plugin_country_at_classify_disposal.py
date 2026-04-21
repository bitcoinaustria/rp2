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

"""Engine-level tests for `classify_disposal` — the API Kassiber consumes to bucket
GainLoss rows into Austrian semantic categories. The mapping from category to BMF
Kennzahl (172/174/175/176/801) lives in Kassiber, so these tests pin the semantic
split rather than any specific form-code assignment.
"""

import unittest
from typing import Dict, List, Optional, cast

from prezzemolo.avl_tree import AVLTree

from rp2.abstract_accounting_method import AbstractAccountingMethod
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import MIN_DATE, Configuration
from rp2.gain_loss import GainLoss
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.accounting_method.moving_average_at import AccountingMethod
from rp2.plugin.country.at import AT, AtDisposalCategory, classify_disposal
from rp2.rp2_decimal import RP2Decimal
from rp2.tax_engine import compute_tax
from rp2.transaction_set import TransactionSet

_ASSET: str = "B1"


def _rp2_decimal(value: str) -> RP2Decimal:
    return RP2Decimal(value)


class TestClassifyDisposal(unittest.TestCase):
    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", AT())

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

    def _earn(self, row: int, timestamp: str, transaction_type: str, crypto_in: str, spot_price: str) -> InTransaction:
        return InTransaction(
            self._configuration,
            timestamp,
            _ASSET,
            "Coinbase",
            "Bob",
            transaction_type,
            _rp2_decimal(spot_price),
            _rp2_decimal(crypto_in),
            fiat_fee=_rp2_decimal("0"),
            row=row,
        )

    def _compute(self, in_txs: List[InTransaction], out_txs: List[OutTransaction]) -> List[GainLoss]:
        in_set: TransactionSet = TransactionSet(self._configuration, "IN", _ASSET)
        for in_tx in in_txs:
            in_set.add_entry(in_tx)
        out_set: TransactionSet = TransactionSet(self._configuration, "OUT", _ASSET)
        for out_tx in out_txs:
            out_set.add_entry(out_tx)
        intra_set: TransactionSet = TransactionSet(self._configuration, "INTRA", _ASSET)
        input_data: InputData = InputData(_ASSET, in_set, out_set, intra_set)
        computed: ComputedData = compute_tax(self._configuration, self._make_engine(), input_data)
        return [cast(GainLoss, gain_loss) for gain_loss in computed.gain_loss_set]

    def _bucket(self, gain_losses: List[GainLoss]) -> Dict[AtDisposalCategory, List[GainLoss]]:
        out: Dict[AtDisposalCategory, List[GainLoss]] = {}
        for gl in gain_losses:
            out.setdefault(classify_disposal(gl), []).append(gl)
        return out

    # ------------------------------------------------------------------ tests ------

    def test_neuvermoegen_gain_routes_to_neu_gain(self) -> None:
        # Pool (2, 400, avg 200). Sell 0.5 @ 500 → cost_basis 100, proceeds 250, gain 150.
        gls = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
            ],
            out_txs=[self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="500")],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.NEU_GAIN, buckets)
        self.assertEqual(len(buckets[AtDisposalCategory.NEU_GAIN]), 1)
        self.assertEqual(buckets[AtDisposalCategory.NEU_GAIN][0].fiat_gain, _rp2_decimal("150"))

    def test_neuvermoegen_loss_routes_to_neu_loss(self) -> None:
        # Buys at 500 & 500 → avg 500. Sell 0.5 @ 100 → loss 200.
        gls = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="500"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="500"),
            ],
            out_txs=[self._sell(row=3, timestamp="2023-03-01 00:00:00 +0000", crypto_out="0.5", spot_price="100")],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.NEU_LOSS, buckets)
        self.assertEqual(buckets[AtDisposalCategory.NEU_LOSS][0].fiat_gain, _rp2_decimal("-200"))

    def test_neuvermoegen_swap_routes_to_neu_swap(self) -> None:
        gls = self._compute(
            in_txs=[
                self._buy(row=1, timestamp="2023-01-01 00:00:00 +0000", crypto_in="1", spot_price="100"),
                self._buy(row=2, timestamp="2023-02-01 00:00:00 +0000", crypto_in="1", spot_price="300"),
            ],
            out_txs=[
                self._sell(
                    row=3,
                    timestamp="2023-03-01 00:00:00 +0000",
                    crypto_out="0.5",
                    spot_price="500",
                    notes="at_swap_link=swap-X",
                ),
            ],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.NEU_SWAP, buckets)
        self.assertNotIn(AtDisposalCategory.NEU_GAIN, buckets)
        self.assertNotIn(AtDisposalCategory.NEU_LOSS, buckets)
        # Zero-gain by construction.
        self.assertEqual(buckets[AtDisposalCategory.NEU_SWAP][0].fiat_gain, _rp2_decimal("0"))

    def test_altvermoegen_within_spekulationsfrist_routes_to_alt_spekulation(self) -> None:
        # Buy 2020-06-01 (alt); sell 2020-12-01 → holding < 365 days.
        gls = self._compute(
            in_txs=[self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[
                self._sell(
                    row=2,
                    timestamp="2020-12-01 00:00:00 +0000",
                    crypto_out="0.5",
                    spot_price="300",
                    notes="at_regime=alt",
                ),
            ],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.ALT_SPEKULATION, buckets)
        self.assertEqual(buckets[AtDisposalCategory.ALT_SPEKULATION][0].fiat_gain, _rp2_decimal("100"))

    def test_altvermoegen_past_spekulationsfrist_routes_to_alt_taxfree(self) -> None:
        # Buy 2019-01-01; sell 2023-06-01 → well past 365 days.
        gls = self._compute(
            in_txs=[self._buy(row=1, timestamp="2019-01-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[
                self._sell(
                    row=2,
                    timestamp="2023-06-01 00:00:00 +0000",
                    crypto_out="0.5",
                    spot_price="500",
                    notes="at_regime=alt",
                ),
            ],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.ALT_TAXFREE, buckets)
        self.assertNotIn(AtDisposalCategory.ALT_SPEKULATION, buckets)

    def test_altvermoegen_exactly_365_day_holding_is_taxfree(self) -> None:
        # Boundary case: Spekulationsfrist threshold is `holding_days >= 365`.
        # Buy 2020-06-01, sell 2021-06-01 → exactly 365 days → tax-free.
        gls = self._compute(
            in_txs=[self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[
                self._sell(
                    row=2,
                    timestamp="2021-06-01 00:00:00 +0000",
                    crypto_out="0.5",
                    spot_price="300",
                    notes="at_regime=alt",
                ),
            ],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.ALT_TAXFREE, buckets)
        self.assertNotIn(AtDisposalCategory.ALT_SPEKULATION, buckets)

    def test_altvermoegen_one_day_short_of_spekulationsfrist_is_spekulation(self) -> None:
        # 2020-06-01 → 2021-05-31 = 364 days → still within Spekulationsfrist.
        gls = self._compute(
            in_txs=[self._buy(row=1, timestamp="2020-06-01 00:00:00 +0000", crypto_in="1", spot_price="100")],
            out_txs=[
                self._sell(
                    row=2,
                    timestamp="2021-05-31 00:00:00 +0000",
                    crypto_out="0.5",
                    spot_price="300",
                    notes="at_regime=alt",
                ),
            ],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.ALT_SPEKULATION, buckets)
        self.assertNotIn(AtDisposalCategory.ALT_TAXFREE, buckets)

    def test_staking_income_routes_to_capital_yield(self) -> None:
        # STAKING is an earn event with no acquired_lot; routes to INCOME_CAPITAL_YIELD
        # (currently mapped to Kz 175 by consumers).
        gls = self._compute(
            in_txs=[self._earn(row=1, timestamp="2023-04-01 00:00:00 +0000", transaction_type="STAKING", crypto_in="0.25", spot_price="200")],
            out_txs=[],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.INCOME_CAPITAL_YIELD, buckets)
        self.assertNotIn(AtDisposalCategory.INCOME_GENERAL, buckets)

    def test_interest_income_routes_to_capital_yield(self) -> None:
        gls = self._compute(
            in_txs=[self._earn(row=1, timestamp="2023-04-01 00:00:00 +0000", transaction_type="INTEREST", crypto_in="0.1", spot_price="200")],
            out_txs=[],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.INCOME_CAPITAL_YIELD, buckets)

    def test_mining_income_routes_to_general(self) -> None:
        # MINING stays on INCOME_GENERAL (currently Kz 172).
        gls = self._compute(
            in_txs=[self._earn(row=1, timestamp="2023-04-01 00:00:00 +0000", transaction_type="MINING", crypto_in="0.5", spot_price="200")],
            out_txs=[],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.INCOME_GENERAL, buckets)
        self.assertNotIn(AtDisposalCategory.INCOME_CAPITAL_YIELD, buckets)

    def test_airdrop_income_routes_to_general(self) -> None:
        gls = self._compute(
            in_txs=[self._earn(row=1, timestamp="2023-04-01 00:00:00 +0000", transaction_type="AIRDROP", crypto_in="1", spot_price="50")],
            out_txs=[],
        )
        buckets = self._bucket(gls)
        self.assertIn(AtDisposalCategory.INCOME_GENERAL, buckets)


if __name__ == "__main__":
    unittest.main()
