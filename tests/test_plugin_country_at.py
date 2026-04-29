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

import sys
import unittest
from typing import List, Optional, Set

from rp2.configuration import Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.out_transaction import OutTransaction
from rp2.plugin.country.at import (
    AT,
    AT_DEFAULT_POOL,
    REGIME_ALT,
    REGIME_NEU,
    classify_lot_regime,
    event_has_explicit_regime,
    has_swap_link,
    pool_id_from_notes,
    rp2_entry,
    swap_link_id,
    validate_at_swap_link_pairing,
)
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2ValueError
from rp2.transaction_set import TransactionSet


class TestPluginCountryAT(unittest.TestCase):
    def setUp(self) -> None:
        self.country = AT()

    def test_iso_codes(self) -> None:
        self.assertEqual(self.country.country_iso_code, "at")
        self.assertEqual(self.country.currency_iso_code, "eur")

    def test_long_term_capital_gain_period_is_disabled(self) -> None:
        # Austria has no generic day-threshold; regime-specific handling lives in the
        # Austrian accounting method.
        self.assertEqual(self.country.get_long_term_capital_gain_period(), sys.maxsize)

    def test_accounting_methods(self) -> None:
        expected: Set[str] = {"fifo", "moving_average", "moving_average_at"}
        self.assertEqual(self.country.get_accounting_methods(), expected)
        self.assertEqual(self.country.get_default_accounting_method(), "moving_average_at")

    def test_report_generators(self) -> None:
        # AT ships only `open_positions` from rp2; the BMF E 1kv layout lives in Kassiber.
        # `rp2_full_report` is intentionally excluded: AT disables the day-threshold
        # (sys.maxsize), so the generic report would collapse Altvermögen into a single
        # short-term bucket and mislead taxpayers.
        expected: Set[str] = {"open_positions"}
        self.assertEqual(self.country.get_report_generators(), expected)

    def test_default_generation_language(self) -> None:
        # de_AT reporting is Kassiber's responsibility; rp2's default falls back to English.
        self.assertEqual(self.country.get_default_generation_language(), "en")

    def test_entry_point_is_callable(self) -> None:
        # Verifies the console-script target resolves and is wired to rp2_main.
        self.assertTrue(callable(rp2_entry))


class TestAtMarkerParsing(unittest.TestCase):
    """`notes` is a free-form field; marker parsing must match exact tokens only.

    Substring matching would let unrelated text flip the regime or trigger Neu swap
    neutrality by accident. On the Neu swap path that becomes silent tax underreporting,
    so these tests pin the tokenized behavior and the duplicate/conflict rejection.
    """

    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", AT())

    def _lot(self, timestamp: str, notes: Optional[str]) -> InTransaction:
        return InTransaction(
            self._configuration,
            timestamp,
            "B1",
            "Coinbase",
            "Bob",
            "BUY",
            RP2Decimal("100"),
            RP2Decimal("1"),
            fiat_fee=RP2Decimal("0"),
            row=1,
            notes=notes,
        )

    def _sell(self, notes: Optional[str]) -> OutTransaction:
        return OutTransaction(
            self._configuration,
            "2023-06-01 00:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "SELL",
            RP2Decimal("500"),
            RP2Decimal("0.5"),
            RP2Decimal("0"),
            row=2,
            notes=notes,
        )

    # ----- exact-token regime matching (no substring false positives) -----

    def test_regime_marker_only_matches_exact_token(self) -> None:
        # Post-cutoff lot tagged with a look-alike `at_regime=altish`: the token is not
        # `at_regime=alt`, so the regime falls back to the date-derived Neu classification.
        lot = self._lot("2023-06-01 00:00:00 +0000", "at_regime=altish")
        self.assertEqual(classify_lot_regime(lot), REGIME_NEU)

    def test_regime_marker_embedded_in_other_word_does_not_match(self) -> None:
        # `prefixed_at_regime=alt` is a single token that doesn't start with `at_regime=`,
        # so it must not flip a post-cutoff lot to Alt.
        lot = self._lot("2023-06-01 00:00:00 +0000", "prefixed_at_regime=alt")
        self.assertEqual(classify_lot_regime(lot), REGIME_NEU)

    def test_regime_marker_with_valid_other_tokens_matches(self) -> None:
        # Multiple markers separated by whitespace/commas must all tokenize.
        lot = self._lot("2023-06-01 00:00:00 +0000", "note text, at_regime=alt, at_pool=cold")
        self.assertEqual(classify_lot_regime(lot), REGIME_ALT)
        self.assertEqual(pool_id_from_notes(lot.notes), "cold")

    def test_conflicting_regime_markers_raise(self) -> None:
        lot = self._lot("2023-06-01 00:00:00 +0000", "at_regime=alt at_regime=neu")
        with self.assertRaisesRegex(RP2ValueError, "Conflicting `at_regime` markers"):
            classify_lot_regime(lot)

    def test_duplicate_regime_markers_raise(self) -> None:
        lot = self._lot("2023-06-01 00:00:00 +0000", "at_regime=alt at_regime=alt")
        with self.assertRaisesRegex(RP2ValueError, "Duplicate `at_regime=alt` markers"):
            classify_lot_regime(lot)

    def test_event_has_explicit_regime_requires_exact_token(self) -> None:
        self.assertFalse(event_has_explicit_regime(self._sell("at_regime=altish")))
        self.assertTrue(event_has_explicit_regime(self._sell("at_regime=alt")))

    # ----- exact-token key=value markers -----

    def test_pool_marker_embedded_in_other_word_does_not_match(self) -> None:
        # `prefixed_at_pool=hot` is a single token not starting with `at_pool=`; default pool wins.
        self.assertEqual(pool_id_from_notes("prefixed_at_pool=hot"), AT_DEFAULT_POOL)

    def test_duplicate_pool_markers_raise(self) -> None:
        with self.assertRaisesRegex(RP2ValueError, r"Duplicate `at_pool=` markers"):
            pool_id_from_notes("at_pool=A at_pool=B")

    def test_swap_link_marker_embedded_in_other_word_does_not_match(self) -> None:
        # A token `prefixed_at_swap_link=x` must not activate swap neutrality. The substring
        # parser would have matched here and silently zeroed the disposal's gain.
        event = self._sell("prefixed_at_swap_link=abc")
        self.assertFalse(has_swap_link(event))
        self.assertIsNone(swap_link_id(event))

    def test_swap_link_marker_matches_at_token_boundary(self) -> None:
        event = self._sell("at_swap_link=abc")
        self.assertTrue(has_swap_link(event))
        self.assertEqual(swap_link_id(event), "abc")

    def test_duplicate_swap_link_markers_raise(self) -> None:
        event = self._sell("at_swap_link=a at_swap_link=b")
        with self.assertRaisesRegex(RP2ValueError, r"Duplicate `at_swap_link=` markers"):
            swap_link_id(event)

    def test_tokenizer_handles_all_documented_separators(self) -> None:
        # Per AGENTS.md, markers may be separated by any of ` \t\n,`. Verify a single notes
        # string carrying every separator still tokenizes correctly.
        lot = self._lot("2023-06-01 00:00:00 +0000", "at_regime=alt\tat_pool=hot\nnote,free")
        self.assertEqual(classify_lot_regime(lot), REGIME_ALT)
        self.assertEqual(pool_id_from_notes(lot.notes), "hot")


class TestAtSwapLinkPairing(unittest.TestCase):
    """Cross-asset validator: every `at_swap_link=<id>` marker must appear on both legs.

    Kassiber annotates both legs when both rows are present, but cannot annotate a row it
    never saw. An orphan outgoing marker would silently produce a zero-gain row with no
    corresponding basis carry on any asset — a bug Kassiber structurally cannot catch.
    These tests pin the pairing invariant RP2 enforces on its side.
    """

    _configuration: Configuration

    @classmethod
    def setUpClass(cls) -> None:
        cls._configuration = Configuration("./config/test_data.ini", AT())

    def _in(self, asset: str, row: int, notes: Optional[str] = None) -> InTransaction:
        return InTransaction(
            self._configuration,
            "2023-03-01 00:00:00 +0000",
            asset,
            "Coinbase",
            "Bob",
            "BUY",
            RP2Decimal("100"),
            RP2Decimal("1"),
            fiat_fee=RP2Decimal("0"),
            row=row,
            notes=notes,
        )

    def _out(self, asset: str, row: int, notes: Optional[str] = None) -> OutTransaction:
        return OutTransaction(
            self._configuration,
            "2023-03-01 00:00:00 +0000",
            asset,
            "Coinbase",
            "Bob",
            "SELL",
            RP2Decimal("500"),
            RP2Decimal("0.5"),
            RP2Decimal("0"),
            row=row,
            notes=notes,
        )

    def _input_data(self, asset: str, in_txs: List[InTransaction], out_txs: List[OutTransaction]) -> InputData:
        in_set: TransactionSet = TransactionSet(self._configuration, "IN", asset)
        for in_tx in in_txs:
            in_set.add_entry(in_tx)
        out_set: TransactionSet = TransactionSet(self._configuration, "OUT", asset)
        for out_tx in out_txs:
            out_set.add_entry(out_tx)
        intra_set: TransactionSet = TransactionSet(self._configuration, "INTRA", asset)
        return InputData(asset, in_set, out_set, intra_set)

    def test_empty_input_list_is_noop(self) -> None:
        validate_at_swap_link_pairing([])

    def test_no_markers_is_noop(self) -> None:
        validate_at_swap_link_pairing(
            [
                self._input_data("B1", [self._in("B1", 1)], [self._out("B1", 2)]),
                self._input_data("B2", [self._in("B2", 1)], []),
            ]
        )

    def test_paired_markers_across_two_assets_pass(self) -> None:
        # Happy path: outgoing on B1, incoming on B2, same swap id.
        validate_at_swap_link_pairing(
            [
                self._input_data("B1", [self._in("B1", 1)], [self._out("B1", 2, notes="at_swap_link=swap-X")]),
                self._input_data("B2", [self._in("B2", 1, notes="at_swap_link=swap-X")], []),
            ]
        )

    def test_orphan_outgoing_marker_raises(self) -> None:
        # The exact class of bug Kassiber cannot catch: outgoing leg marked but no incoming.
        with self.assertRaisesRegex(RP2ValueError, r"Unpaired `at_swap_link=swap-X`"):
            validate_at_swap_link_pairing(
                [
                    self._input_data("B1", [self._in("B1", 1)], [self._out("B1", 2, notes="at_swap_link=swap-X")]),
                    self._input_data("B2", [self._in("B2", 1)], []),
                ]
            )

    def test_orphan_incoming_marker_raises(self) -> None:
        # Symmetric: incoming marked but no outgoing. Also a Kassiber bug.
        with self.assertRaisesRegex(RP2ValueError, r"Unpaired `at_swap_link=swap-Y`"):
            validate_at_swap_link_pairing(
                [
                    self._input_data("B1", [self._in("B1", 1)], []),
                    self._input_data("B2", [self._in("B2", 1, notes="at_swap_link=swap-Y")], []),
                ]
            )

    def test_same_asset_pair_raises(self) -> None:
        # A crypto-to-crypto swap crosses two assets by definition. Both legs on the same
        # asset indicates a misclassified same-asset transfer or a Kassiber emission bug.
        with self.assertRaisesRegex(RP2ValueError, r"same-asset"):
            validate_at_swap_link_pairing(
                [
                    self._input_data(
                        "B1",
                        [self._in("B1", 1, notes="at_swap_link=swap-Z")],
                        [self._out("B1", 2, notes="at_swap_link=swap-Z")],
                    ),
                ]
            )

    def test_alt_marker_is_skipped_not_validated(self) -> None:
        # Austrian law ignores `at_swap_link` on Alt disposals — they realize as normal
        # taxable gains. Validating pairing there would hard-fail the legitimate
        # `at_regime=alt at_swap_link=...` case that the accounting method already handles
        # correctly. The validator must skip events explicitly tagged `at_regime=alt`.
        validate_at_swap_link_pairing(
            [
                self._input_data(
                    "B1",
                    [self._in("B1", 1)],
                    [self._out("B1", 2, notes="at_regime=alt at_swap_link=ignored")],
                ),
            ]
        )

    def test_alt_incoming_leg_is_also_skipped(self) -> None:
        # Symmetric rule: an incoming leg tagged `at_regime=alt at_swap_link=...` is also a
        # no-op in the accounting method, so the validator must not require it to pair.
        validate_at_swap_link_pairing(
            [
                self._input_data(
                    "B1",
                    [self._in("B1", 1, notes="at_regime=alt at_swap_link=orphan")],
                    [],
                ),
            ]
        )

    def test_mixed_neu_pair_and_alt_orphan_validates(self) -> None:
        # Realistic two-asset scenario: a valid Neu swap between B1 and B2, plus a separate
        # Alt disposal carrying `at_swap_link` for Kassiber's own bookkeeping. The Alt marker
        # is skipped; the Neu pair validates cleanly.
        validate_at_swap_link_pairing(
            [
                self._input_data(
                    "B1",
                    [self._in("B1", 1)],
                    [
                        self._out("B1", 2, notes="at_swap_link=neu-swap"),
                        self._out("B1", 3, notes="at_regime=alt at_swap_link=alt-bookkeeping"),
                    ],
                ),
                self._input_data("B2", [self._in("B2", 1, notes="at_swap_link=neu-swap")], []),
            ]
        )

    def test_duplicate_outgoing_markers_raise(self) -> None:
        # Two outgoing legs with the same swap id is not a valid pair — one outgoing, one
        # incoming on different assets is the only shape RP2 honors.
        with self.assertRaisesRegex(RP2ValueError, r"Unpaired `at_swap_link=swap-D`.*found 2 outgoing"):
            validate_at_swap_link_pairing(
                [
                    self._input_data(
                        "B1",
                        [self._in("B1", 1)],
                        [self._out("B1", 2, notes="at_swap_link=swap-D"), self._out("B1", 3, notes="at_swap_link=swap-D")],
                    ),
                    self._input_data("B2", [self._in("B2", 4, notes="at_swap_link=swap-D")], []),
                ]
            )

    def test_empty_swap_link_marker_raises_in_pairing_validator(self) -> None:
        with self.assertRaisesRegex(RP2ValueError, r"Empty `at_swap_link=` marker"):
            validate_at_swap_link_pairing(
                [
                    self._input_data("B1", [self._in("B1", 1)], [self._out("B1", 2, notes="at_swap_link=")]),
                    self._input_data("B2", [self._in("B2", 3)], []),
                ]
            )

    def test_incoming_before_outgoing_marker_raises(self) -> None:
        early_in = InTransaction(
            self._configuration,
            "2023-02-01 00:00:00 +0000",
            "B2",
            "Coinbase",
            "Bob",
            "BUY",
            RP2Decimal("100"),
            RP2Decimal("1"),
            fiat_fee=RP2Decimal("0"),
            row=1,
            notes="at_swap_link=time-bug",
        )
        late_out = OutTransaction(
            self._configuration,
            "2023-03-01 00:00:00 +0000",
            "B1",
            "Coinbase",
            "Bob",
            "SELL",
            RP2Decimal("500"),
            RP2Decimal("0.5"),
            RP2Decimal("0"),
            row=2,
            notes="at_swap_link=time-bug",
        )
        with self.assertRaisesRegex(RP2ValueError, r"incoming leg is earlier"):
            validate_at_swap_link_pairing(
                [
                    self._input_data("B1", [self._in("B1", 3)], [late_out]),
                    self._input_data("B2", [early_in], []),
                ]
            )


if __name__ == "__main__":
    unittest.main()
