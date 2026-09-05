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
from typing import TYPE_CHECKING, Iterator

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
    _reject_cyclic_swap_dependencies(pairs, incoming_pairs_by_asset)

    basis_overrides_by_asset: dict[str, dict[InTransaction, RP2Decimal]] = {asset: {} for asset in asset_to_input_data}
    resolved_incoming_by_asset: dict[str, set[InTransaction]] = {asset: set() for asset in asset_to_input_data}
    cursors: dict[str, TaxEngineCursor] = {
        asset: TaxEngineCursor(
            configuration=configuration,
            accounting_engine=accounting_engine,
            input_data=input_data,
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
                if result.taxable_event_unit_cost_basis is None and _consumed_neu_lot(result):
                    # The outgoing leg consumed Neuvermögen lots but the accounting method produced no
                    # carried cost basis. Only `moving_average_at` honors the swap marker on the Neu
                    # path — it emits the zero-gain override and the Neu pool average to carry. fifo /
                    # plain moving_average would silently realize a taxable gain on the outgoing leg
                    # while classify_disposal still buckets it NEU_SWAP, and carry no basis to the
                    # incoming leg. Fail loudly instead of under-reporting. (An *unmarked* swap row
                    # routed to the Alt path consumes Alt lots and legitimately carries nothing — Alt
                    # swaps are regime-breaking taxable disposals — so it is resolved without a carry
                    # below rather than rejected.)
                    raise RP2ValueError(
                        f"Austrian swap neutrality requires the `moving_average_at` accounting method, but the Neuvermögen outgoing leg of "
                        f"at_swap_link={source_pair.swap_id} ({source_pair.out_asset} {source_pair.out_transaction.internal_id}) produced no "
                        f"carried cost basis. The configured accounting method did not honor the swap marker. Re-run with `-m moving_average_at` "
                        f"(the AT default), or remove the at_swap_link markers if you intend swaps to be taxable disposals."
                    )
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


def _reject_cyclic_swap_dependencies(
    swap_pairs: dict[str, AtSwapPair],
    incoming_pairs_by_asset: dict[str, list[AtSwapPair]],
) -> None:
    dependency_graph: dict[str, set[str]] = {swap_id: set() for swap_id in swap_pairs}
    for pair in swap_pairs.values():
        for blocker in incoming_pairs_by_asset.get(pair.out_asset, []):
            if blocker.swap_id == pair.swap_id:
                continue
            if blocker.in_transaction.timestamp > pair.out_transaction.timestamp:
                continue
            if _incoming_can_affect_event(blocker.in_transaction, pair.out_transaction):
                dependency_graph[pair.swap_id].add(blocker.swap_id)

    cycle: list[str] | None = _find_dependency_cycle(dependency_graph)
    if cycle is not None:
        cycle_summary: str = " -> ".join(f"at_swap_link={swap_id}" for swap_id in cycle)
        raise RP2ValueError(
            "Cyclic Austrian swap basis dependency: same-pool Neu swap legs cannot carry basis before their own "
            f"incoming leg has been resolved ({cycle_summary}). Add distinct at_pool markers for independent pools, "
            "or split the transactions into an order that does not require using unresolved carried basis."
        )


def _find_dependency_cycle(dependency_graph: dict[str, set[str]]) -> list[str] | None:
    visited: set[str] = set()
    active: set[str] = set()
    stack: list[str] = []
    dependencies: dict[str, Iterator[str]] = {}

    for node in sorted(dependency_graph):
        if node in visited:
            continue
        active.add(node)
        stack.append(node)
        dependencies[node] = iter(sorted(dependency_graph.get(node, set())))
        # Keep DFS frames explicitly: a valid chronological swap chain can be longer than
        # Python's recursion limit. Sorted iterators preserve the existing cycle diagnostic.
        while stack:
            current = stack[-1]
            dependency = next(dependencies[current], None)
            if dependency is None:
                stack.pop()
                active.remove(current)
                visited.add(current)
                del dependencies[current]
                continue
            if dependency in active:
                cycle_start: int = stack.index(dependency)
                return stack[cycle_start:] + [dependency]
            if dependency in visited:
                continue
            active.add(dependency)
            stack.append(dependency)
            dependencies[dependency] = iter(sorted(dependency_graph.get(dependency, set())))
    return None


def _consumed_neu_lot(result: TaxableEventComputation) -> bool:
    # True if any lot the disposal consumed is Neuvermögen. Used to tell a misconfigured method
    # (fifo / plain moving_average on a Neu swap — must fail) apart from an unmarked swap row that
    # moving_average_at legitimately routed to the Alt path (Alt swaps are taxable and carry no
    # basis — must resolve without a carry, not fail).
    # pylint: disable=import-outside-toplevel
    from rp2.plugin.country.at import REGIME_NEU, classify_lot_regime

    return any(gain_loss.acquired_lot is not None and classify_lot_regime(gain_loss.acquired_lot) == REGIME_NEU for gain_loss in result.gain_losses)


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
    # Only an *explicit* at_regime=alt event is known not to touch the Neu pool. An unmarked event
    # that would route to Alt purely by lot availability is treated conservatively as Neu-affecting,
    # because this layer has no per-pool lot-availability view (that lives in moving_average_at). The
    # cost of the conservatism is at most a spurious "Unable to order ..." error in a contrived swap
    # cycle where every asset's only progress depends on an unmarked-Alt disposal — never a wrong
    # number. The fix (disambiguate that disposal with at_regime=alt) is in the caller's hands, and
    # making this precise would require threading lot availability across assets, so we fail loud
    # rather than risk mis-ordering the basis carry.
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
