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

import logging
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Tuple, Union, cast

import ezodf

from rp2.abstract_country import AbstractCountry
from rp2.abstract_report_generator import AbstractReportGenerator
from rp2.computed_data import ComputedData
from rp2.entry_types import TransactionType
from rp2.gain_loss import GainLoss
from rp2.localization import _
from rp2.logger import create_logger
from rp2.plugin.country.at import (
    AT_SPEKULATIONSFRIST_DAYS,
    REGIME_NEU,
    classify_lot_regime,
    has_swap_link,
    swap_link_id,
)
from rp2.rp2_decimal import ZERO, RP2Decimal
from rp2.rp2_error import RP2RuntimeError, RP2TypeError

LOGGER: logging.Logger = create_logger("tax_report_at")

# Category keys used to bucket each GainLoss row into its reporting lane. These are internal
# identifiers, not user-facing strings — they are not localized.
_CAT_INCOME_172: str = "income_172"
_CAT_INCOME_175: str = "income_175"
_CAT_NEU_GAIN: str = "neu_gain"
_CAT_NEU_LOSS: str = "neu_loss"
_CAT_NEU_SWAP: str = "neu_swap"
_CAT_ALT_SPEKULATION: str = "alt_spekulation"
_CAT_ALT_TAXFREE: str = "alt_taxfree"

# Austrian BMF income classification of crypto earn events.
# Kz 175 (Einkünfte aus der Überlassung): lending/staking — user gives crypto to a third
# party for a return. Kz 172 (Laufende Einkünfte): everything else that rp2 models as an earn
# event (mining, airdrops, hardforks, generic income, wages-in-crypto fallback).
_KZ_175_TRANSACTION_TYPES: frozenset[TransactionType] = frozenset({TransactionType.STAKING, TransactionType.INTEREST})


def _classify(gain_loss: GainLoss) -> str:
    if gain_loss.acquired_lot is None:
        if gain_loss.taxable_event.transaction_type in _KZ_175_TRANSACTION_TYPES:
            return _CAT_INCOME_175
        return _CAT_INCOME_172
    regime: str = classify_lot_regime(gain_loss.acquired_lot)
    if regime == REGIME_NEU:
        if has_swap_link(gain_loss.taxable_event):
            return _CAT_NEU_SWAP
        return _CAT_NEU_GAIN if gain_loss.fiat_gain >= ZERO else _CAT_NEU_LOSS
    # Alt
    holding_days: int = (gain_loss.taxable_event.timestamp - gain_loss.acquired_lot.timestamp).days
    if holding_days >= AT_SPEKULATIONSFRIST_DAYS:
        return _CAT_ALT_TAXFREE
    return _CAT_ALT_SPEKULATION


def _fmt_eur(value: RP2Decimal) -> str:
    return f"{value:.2f}"


def _fmt_crypto(value: RP2Decimal) -> str:
    return f"{value:.8f}"


def _fmt_date(value: Union[date, datetime]) -> str:
    return value.strftime("%Y-%m-%d")


class Generator(AbstractReportGenerator):
    OUTPUT_FILE: str = "tax_report_at.ods"

    def generate(  # pylint: disable=too-many-locals
        self,
        country: AbstractCountry,
        years_2_accounting_method_names: Dict[int, str],
        asset_to_computed_data: Dict[str, ComputedData],
        output_dir_path: str,
        output_file_prefix: str,
        from_date: date,
        to_date: date,
        generation_language: str,  # pylint: disable=unused-argument
    ) -> None:
        if not isinstance(asset_to_computed_data, dict):
            raise RP2TypeError(f"Parameter 'asset_to_computed_data' has non-Dict value {asset_to_computed_data}")
        AbstractCountry.type_check("country", country)

        # Aggregate per-category and capture per-row detail for the listing sheets.
        totals: DefaultDict[str, RP2Decimal] = defaultdict(lambda: ZERO)
        rows: DefaultDict[str, List[Tuple[str, GainLoss]]] = defaultdict(list)

        for asset, computed_data in asset_to_computed_data.items():
            if not isinstance(asset, str):
                raise RP2TypeError(f"Parameter 'asset' has non-string value {asset}")
            ComputedData.type_check("computed_data", computed_data)
            for entry in computed_data.gain_loss_set:
                gain_loss: GainLoss = cast(GainLoss, entry)
                category: str = _classify(gain_loss)
                rows[category].append((asset, gain_loss))
                if category == _CAT_INCOME_172:
                    totals[_CAT_INCOME_172] = totals[_CAT_INCOME_172] + gain_loss.taxable_event_fiat_amount_with_fee_fraction
                elif category == _CAT_INCOME_175:
                    totals[_CAT_INCOME_175] = totals[_CAT_INCOME_175] + gain_loss.taxable_event_fiat_amount_with_fee_fraction
                elif category == _CAT_NEU_GAIN:
                    totals[_CAT_NEU_GAIN] = totals[_CAT_NEU_GAIN] + gain_loss.fiat_gain
                elif category == _CAT_NEU_LOSS:
                    # Kennzahl 176 is reported as a positive magnitude.
                    totals[_CAT_NEU_LOSS] = totals[_CAT_NEU_LOSS] + (-gain_loss.fiat_gain)
                elif category == _CAT_ALT_SPEKULATION:
                    # Kennzahl 801 is reported as the net (may be positive or negative).
                    totals[_CAT_ALT_SPEKULATION] = totals[_CAT_ALT_SPEKULATION] + gain_loss.fiat_gain
                elif category == _CAT_ALT_TAXFREE:
                    totals[_CAT_ALT_TAXFREE] = totals[_CAT_ALT_TAXFREE] + gain_loss.fiat_gain
                # _CAT_NEU_SWAP deliberately contributes nothing to taxable totals.

        accounting_method: str = next(iter(years_2_accounting_method_names.values())) if len(years_2_accounting_method_names) == 1 else "mixed"
        output_file_path: Path = Path(output_dir_path) / f"{output_file_prefix}{accounting_method}_{self.OUTPUT_FILE}"
        if output_file_path.exists():
            output_file_path.unlink()

        doc: Any = ezodf.newdoc("ods", str(output_file_path))
        self._add_summary_sheet(doc, country, accounting_method, from_date, to_date, totals, rows)
        self._add_finanzonline_sheet(doc, totals)
        self._add_disposals_sheet(doc, _("Neuvermoegen Disposals"), rows[_CAT_NEU_GAIN] + rows[_CAT_NEU_LOSS], include_holding_days=False)
        self._add_disposals_sheet(doc, _("Altvermoegen Disposals"), rows[_CAT_ALT_SPEKULATION] + rows[_CAT_ALT_TAXFREE], include_holding_days=True)
        self._add_swaps_sheet(doc, rows[_CAT_NEU_SWAP])
        self._add_income_sheet(doc, _("Income (Kz 172)"), rows[_CAT_INCOME_172])
        self._add_income_sheet(doc, _("Income (Kz 175)"), rows[_CAT_INCOME_175])
        doc.save()
        LOGGER.info("Plugin '%s' output: %s", __name__, Path(output_file_path).resolve())

    @staticmethod
    def _set_row(sheet: Any, row_index: int, values: List[str]) -> None:
        for col_index, value in enumerate(values):
            sheet[row_index, col_index].set_value(value)

    def _add_summary_sheet(
        self,
        doc: Any,
        country: AbstractCountry,
        accounting_method: str,
        from_date: date,
        to_date: date,
        totals: DefaultDict[str, RP2Decimal],
        rows: DefaultDict[str, List[Tuple[str, GainLoss]]],
    ) -> None:
        lines: List[Tuple[str, str]] = [
            (_("Austrian Tax Report (Muster / informational)"), ""),
            (_("Country"), country.country_iso_code.upper()),
            (_("Fiat currency"), country.currency_iso_code.upper()),
            (_("Accounting method"), accounting_method),
            (_("Period from"), _fmt_date(from_date)),
            (_("Period to"), _fmt_date(to_date)),
            ("", ""),
            (_("=== Kennzahlen Totals (EUR) ==="), ""),
            (_("Kz 172 - Laufende Einkuenfte (foreign)"), _fmt_eur(totals[_CAT_INCOME_172])),
            (_("Kz 174 - Realized gains Neuvermoegen (foreign)"), _fmt_eur(totals[_CAT_NEU_GAIN])),
            (_("Kz 175 - Einkuenfte aus Ueberlassung (foreign)"), _fmt_eur(totals[_CAT_INCOME_175])),
            (_("Kz 176 - Losses Neuvermoegen (foreign, positive magnitude)"), _fmt_eur(totals[_CAT_NEU_LOSS])),
            (_("Kz 801 - Spekulationsgeschaefte (Altvermoegen < 1 year, net)"), _fmt_eur(totals[_CAT_ALT_SPEKULATION])),
            ("", ""),
            (_("=== Informational ==="), ""),
            (_("Altvermoegen tax-free (>= 1 year holding)"), _fmt_eur(totals[_CAT_ALT_TAXFREE])),
            (_("Neuvermoegen tax-neutral swaps (count)"), str(len(rows[_CAT_NEU_SWAP]))),
            ("", ""),
            (_("=== Disclaimer ==="), ""),
            (
                _(
                    "This report is an automated summary of imported transactions under the assumption that "
                    "all disposals are foreign (auslaendisch). Kennzahlen 171/173 (inlaendisch, CASP-withheld) "
                    "are left blank and must be transcribed manually from the domestic provider's own tax "
                    "statement. Kz 175 income is routed from STAKING and INTEREST transaction types only; "
                    "reclassify other rewards in Kassiber if your case requires it. Have a Steuerberater "
                    "review this output before filing on FinanzOnline."
                ),
                "",
            ),
        ]
        sheet: Any = ezodf.Sheet(_("Summary"), size=(len(lines), 2))
        doc.sheets += sheet
        for row_index, (label, value) in enumerate(lines):
            self._set_row(sheet, row_index, [label, value])

    def _add_finanzonline_sheet(self, doc: Any, totals: DefaultDict[str, RP2Decimal]) -> None:
        # Kennzahl codes and their German labels are the BMF form's verbatim field names —
        # never localized, because the taxpayer transcribes them unchanged into FinanzOnline.
        # Only the surrounding column/section headers and the inlaendisch placeholder are
        # localized.
        rows: List[List[str]] = [
            [_("Kennzahl"), _("Label"), _("Value (EUR)")],
            ["", "Einkuenfte aus Kapitalvermoegen (auslaendisch)", ""],
            ["172", "Laufende Einkuenfte", _fmt_eur(totals[_CAT_INCOME_172])],
            ["174", "Ueberschuesse aus realisierten Wertsteigerungen", _fmt_eur(totals[_CAT_NEU_GAIN])],
            ["175", "Einkuenfte aus der Ueberlassung von Kryptowaehrungen", _fmt_eur(totals[_CAT_INCOME_175])],
            ["176", "Verluste", _fmt_eur(totals[_CAT_NEU_LOSS])],
            ["", "", ""],
            ["", "Einkuenfte aus Kapitalvermoegen (inlaendisch, CASP-withheld)", ""],
            ["171", "Laufende Einkuenfte", _("(transcribe from domestic provider)")],
            ["173", "Ueberschuesse aus realisierten Wertsteigerungen", _("(transcribe from domestic provider)")],
            ["", "", ""],
            ["", "Sonstige Einkuenfte", ""],
            ["801", "Einkuenfte aus Spekulationsgeschaeften (Altvermoegen < 1y)", _fmt_eur(totals[_CAT_ALT_SPEKULATION])],
        ]
        sheet: Any = ezodf.Sheet(_("FinanzOnline"), size=(len(rows), 3))
        doc.sheets += sheet
        for row_index, values in enumerate(rows):
            self._set_row(sheet, row_index, values)

    def _add_disposals_sheet(
        self,
        doc: Any,
        sheet_name: str,
        entries: List[Tuple[str, GainLoss]],
        include_holding_days: bool,
    ) -> None:
        header: List[str] = [
            _("Disposal date"),
            _("Asset"),
            _("Crypto amount"),
            _("Proceeds EUR"),
            _("Cost basis EUR"),
            _("Gain/Loss EUR"),
            _("Acquisition date"),
            _("Acquired lot ID"),
            _("Disposal Tx ID"),
        ]
        if include_holding_days:
            header += [_("Holding days"), _("Status")]
        rows: List[List[str]] = [header]
        for asset, gl in entries:
            if gl.acquired_lot is None:
                raise RP2RuntimeError("Internal error: disposal entry has no acquired_lot")
            row: List[str] = [
                _fmt_date(gl.taxable_event.timestamp),
                asset,
                _fmt_crypto(gl.crypto_amount),
                _fmt_eur(gl.taxable_event_fiat_amount_with_fee_fraction),
                _fmt_eur(gl.fiat_cost_basis),
                _fmt_eur(gl.fiat_gain),
                _fmt_date(gl.acquired_lot.timestamp),
                gl.acquired_lot.unique_id or "",
                gl.taxable_event.unique_id or "",
            ]
            if include_holding_days:
                holding_days: int = (gl.taxable_event.timestamp - gl.acquired_lot.timestamp).days
                status: str = _("TAX-FREE") if holding_days >= AT_SPEKULATIONSFRIST_DAYS else _("TAXABLE (Kz 801)")
                row += [str(holding_days), status]
            rows.append(row)
        sheet: Any = ezodf.Sheet(sheet_name, size=(max(1, len(rows)), len(header)))
        doc.sheets += sheet
        for row_index, values in enumerate(rows):
            self._set_row(sheet, row_index, values)

    def _add_swaps_sheet(self, doc: Any, entries: List[Tuple[str, GainLoss]]) -> None:
        header: List[str] = [
            _("Swap date"),
            _("Asset (outgoing)"),
            _("Crypto amount"),
            _("Spot price EUR"),
            _("Proceeds EUR (zero gain by construction)"),
            _("Pool cost basis carried EUR"),
            _("Swap link (at_swap_link)"),
            _("Tx ID"),
        ]
        rows: List[List[str]] = [header]
        for asset, gl in entries:
            link_id: str = swap_link_id(gl.taxable_event) or ""
            rows.append(
                [
                    _fmt_date(gl.taxable_event.timestamp),
                    asset,
                    _fmt_crypto(gl.crypto_amount),
                    _fmt_eur(gl.taxable_event.spot_price),
                    _fmt_eur(gl.taxable_event_fiat_amount_with_fee_fraction),
                    _fmt_eur(gl.fiat_cost_basis),
                    link_id,
                    gl.taxable_event.unique_id or "",
                ]
            )
        sheet: Any = ezodf.Sheet(_("Swaps (Neu, tax-neutral)"), size=(max(1, len(rows)), len(header)))
        doc.sheets += sheet
        for row_index, values in enumerate(rows):
            self._set_row(sheet, row_index, values)

    def _add_income_sheet(self, doc: Any, sheet_name: str, entries: List[Tuple[str, GainLoss]]) -> None:
        header: List[str] = [
            _("Receipt date"),
            _("Asset"),
            _("Crypto amount"),
            _("Fiat value EUR"),
            _("Transaction type"),
            _("Tx ID"),
        ]
        rows: List[List[str]] = [header]
        for asset, gl in entries:
            rows.append(
                [
                    _fmt_date(gl.taxable_event.timestamp),
                    asset,
                    _fmt_crypto(gl.crypto_amount),
                    _fmt_eur(gl.taxable_event_fiat_amount_with_fee_fraction),
                    gl.taxable_event.transaction_type.value,
                    gl.taxable_event.unique_id or "",
                ]
            )
        sheet: Any = ezodf.Sheet(sheet_name, size=(max(1, len(rows)), len(header)))
        doc.sheets += sheet
        for row_index, values in enumerate(rows):
            self._set_row(sheet, row_index, values)
