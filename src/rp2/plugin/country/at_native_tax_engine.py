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

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from rp2.abstract_transaction import AbstractTransaction
from rp2.accounting_engine import AccountingEngine
from rp2.computed_data import ComputedData
from rp2.configuration import Configuration
from rp2.in_transaction import InTransaction
from rp2.input_data import InputData
from rp2.logger import LOGGER
from rp2.rp2_decimal import RP2Decimal
from rp2.rp2_error import RP2RuntimeError, RP2ValueError
from rp2.tax_engine import TaxableEventComputation, TaxEngineCursor

if TYPE_CHECKING:
    from rp2.plugin.country.at import AtSwapPair

_TransactionKey = tuple[str, str]


def compute_native_at_tax(
    configuration: Configuration,
    accounting_engine: AccountingEngine,
    asset_to_input_data: dict[str, InputData],
    swap_pairs: dict[str, AtSwapPair],
) -> dict[str, ComputedData]:
    """Compute Austrian tax while carrying Neu swap basis across assets.

    The regular RP2 engine computes one asset at a time, but Austrian crypto-to-crypto
    swaps need the source asset's moving-average pool state to seed the destination asset's
    incoming lot. This runner still delegates lot selection and pool math to
    ``moving_average_at``; it only interleaves per-asset taxable events enough to pass the
    computed basis from the outgoing leg to the paired incoming leg before that incoming lot
    can enter the destination pool.
    """
    Configuration.type_check("configuration", configuration)
    AccountingEngine.type_check("accounting_engine", accounting_engine)
    for asset, input_data in asset_to_input_data.items():
        Configuration.type_check_string("asset", asset)
        InputData.type_check("input_data", input_data)

    pairs: dict[str, AtSwapPair] = swap_pairs
    source_key_to_pair: dict[_TransactionKey, AtSwapPair] = {_event_key(pair.out_asset, pair.out_transaction): pair for pair in pairs.values()}
    incoming_pairs_by_asset: dict[str, list[AtSwapPair]] = {}
    for swap_pair in pairs.values():
        incoming_pairs_by_asset.setdefault(swap_pair.in_asset, []).append(swap_pair)
    for asset_pairs in incoming_pairs_by_asset.values():
        asset_pairs.sort(key=_swap_pair_sort_key)

    basis_overrides_by_asset: dict[str, dict[InTransaction, RP2Decimal]] = {asset: {} for asset in asset_to_input_data}
    resolved_incoming_by_asset: dict[str, set[InTransaction]] = {asset: set() for asset in asset_to_input_data}
    cursors: dict[str, TaxEngineCursor] = {
        asset: TaxEngineCursor(
            configuration,
            accounting_engine,
            input_data,
            acquired_lot_2_fiat_in_with_fee_override=basis_overrides_by_asset[asset],
        )
        for asset, input_data in asset_to_input_data.items()
    }

    while any(cursor.has_next() for cursor in cursors.values()):
        progressed: bool = False
        for asset, taxable_event in _sorted_current_events(cursors):
            blocker: AtSwapPair | None = _first_unresolved_incoming_pair(
                asset,
                taxable_event,
                incoming_pairs_by_asset,
                resolved_incoming_by_asset,
            )
            if blocker is not None:
                continue

            LOGGER.info("Processing %s", asset)
            result: TaxableEventComputation = cursors[asset].consume_next_taxable_event()
            source_pair: AtSwapPair | None = source_key_to_pair.get(_event_key(asset, result.taxable_event))
            if source_pair is not None:
                _resolve_swap_pair(result, source_pair, basis_overrides_by_asset, resolved_incoming_by_asset)
            progressed = True
            break

        if not progressed:
            blocked_summary: str = _blocked_event_summary(cursors, incoming_pairs_by_asset, resolved_incoming_by_asset)
            raise RP2ValueError(f"Unable to order Austrian swap-linked taxable events without using unresolved carried basis: {blocked_summary}")

    return {asset: cursor.to_computed_data() for asset, cursor in cursors.items()}


def _resolve_swap_pair(
    result: TaxableEventComputation,
    pair: AtSwapPair,
    basis_overrides_by_asset: dict[str, dict[InTransaction, RP2Decimal]],
    resolved_incoming_by_asset: dict[str, set[InTransaction]],
) -> None:
    destination_overrides: dict[InTransaction, RP2Decimal] = basis_overrides_by_asset[pair.in_asset]
    destination_resolved: set[InTransaction] = resolved_incoming_by_asset[pair.in_asset]
    if pair.in_transaction in destination_resolved:
        raise RP2RuntimeError(f"Internal error: swap pair {pair.swap_id} resolved more than once")

    if result.taxable_event_unit_cost_basis is not None:
        carried_basis: RP2Decimal = result.taxable_event.crypto_taxable_amount * result.taxable_event_unit_cost_basis
        destination_overrides[pair.in_transaction] = carried_basis
        LOGGER.debug(
            "Austrian swap %s: carried %s fiat basis from %s %s to %s %s",
            pair.swap_id,
            carried_basis,
            pair.out_asset,
            pair.out_transaction.internal_id,
            pair.in_asset,
            pair.in_transaction.internal_id,
        )
    destination_resolved.add(pair.in_transaction)


def _first_unresolved_incoming_pair(
    asset: str,
    taxable_event: AbstractTransaction,
    incoming_pairs_by_asset: dict[str, list[AtSwapPair]],
    resolved_incoming_by_asset: dict[str, set[InTransaction]],
) -> AtSwapPair | None:
    for pair in incoming_pairs_by_asset.get(asset, []):
        if pair.in_transaction.timestamp > taxable_event.timestamp:
            return None
        if pair.in_transaction not in resolved_incoming_by_asset[asset] and _incoming_can_affect_event(pair.in_transaction, taxable_event):
            return pair
    return None


def _incoming_can_affect_event(in_transaction: InTransaction, taxable_event: AbstractTransaction) -> bool:
    # Keep this import lazy: `at` imports this native runner, while these marker helpers live
    # in `at` to preserve the public Kassiber handoff surface.
    # pylint: disable=import-outside-toplevel
    from rp2.plugin.country.at import (
        REGIME_ALT,
        REGIME_NEU,
        classify_lot_regime,
        event_has_explicit_regime,
        explicit_event_regime,
        pool_id_from_notes,
    )

    if classify_lot_regime(in_transaction) != REGIME_NEU:
        return False
    if event_has_explicit_regime(taxable_event) and explicit_event_regime(taxable_event) == REGIME_ALT:
        return False
    return pool_id_from_notes(in_transaction.notes) == pool_id_from_notes(taxable_event.notes)


def _sorted_current_events(cursors: dict[str, TaxEngineCursor]) -> list[tuple[str, AbstractTransaction]]:
    result: list[tuple[str, AbstractTransaction]] = []
    for asset, cursor in cursors.items():
        taxable_event: AbstractTransaction | None = cursor.current_taxable_event
        if taxable_event is not None:
            result.append((asset, taxable_event))
    return sorted(result, key=_current_event_sort_key)


def _blocked_event_summary(
    cursors: dict[str, TaxEngineCursor],
    incoming_pairs_by_asset: dict[str, list[AtSwapPair]],
    resolved_incoming_by_asset: dict[str, set[InTransaction]],
) -> str:
    details: list[str] = []
    for asset, taxable_event in _sorted_current_events(cursors):
        blocker: AtSwapPair | None = _first_unresolved_incoming_pair(
            asset,
            taxable_event,
            incoming_pairs_by_asset,
            resolved_incoming_by_asset,
        )
        if blocker is None:
            continue
        details.append(
            f"{asset} event {taxable_event.internal_id} at {taxable_event.timestamp.isoformat()} waits for "
            f"at_swap_link={blocker.swap_id} from {blocker.out_asset} event {blocker.out_transaction.internal_id}"
        )
    return "; ".join(details)


def _event_key(asset: str, event: AbstractTransaction) -> _TransactionKey:
    return asset, event.internal_id


def _swap_pair_sort_key(pair: AtSwapPair) -> tuple[tuple[datetime, int, str], str]:
    return _event_sort_key(pair.in_transaction), pair.swap_id


def _current_event_sort_key(item: tuple[str, AbstractTransaction]) -> tuple[tuple[datetime, int, str], str]:
    return _event_sort_key(item[1]), item[0]


def _event_sort_key(event: AbstractTransaction) -> tuple[datetime, int, str]:
    try:
        internal_id_int: int = int(event.internal_id)
    except ValueError:
        internal_id_int = 0
    return event.timestamp, internal_id_int, event.internal_id
