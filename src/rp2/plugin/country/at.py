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
from typing import Set

from rp2.abstract_country import AbstractCountry
from rp2.rp2_main import rp2_main


# Austria-specific class
class AT(AbstractCountry):
    def __init__(self) -> None:
        super().__init__("at", "eur")

    # Measured in days. Austria has no generic long-term threshold: Neuvermögen disposals
    # are taxed at 27.5% regardless of holding period, and Altvermögen's 1-year
    # Spekulationsfrist applies only to pre-2021-03-01 lots. Regime-specific holding-period
    # handling belongs inside the Austrian accounting method (Phase 3+), not here.
    def get_long_term_capital_gain_period(self) -> int:
        return sys.maxsize

    # Default accounting method to use if the user doesn't specify one on the command line.
    # Phase 1 ships FIFO only; `moving_average_at` lands in Phase 2 and becomes the default.
    def get_default_accounting_method(self) -> str:
        return "fifo"

    # Set of accounting methods accepted in the country.
    def get_accounting_methods(self) -> Set[str]:
        return {"fifo"}

    # Default set of generators to use if the user doesn't specify them on the command line.
    # `at.tax_report_at` (E 1kv-aligned) lands in Phase 5.
    def get_report_generators(self) -> Set[str]:
        return {
            "open_positions",
            "rp2_full_report",
        }

    # Default language to use at report generation if the user doesn't specify it on the
    # command line. `de_AT` catalog ships in Phase 6; until then Babel falls back to English.
    def get_default_generation_language(self) -> str:
        return "de_AT"


# Austria-specific entry point
def rp2_entry() -> None:
    rp2_main(AT())
