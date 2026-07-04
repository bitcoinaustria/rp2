# Copyright 2022 macanudo527
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

_UNSUPPORTED_ACCOUNTING_MESSAGE: str = (
    "Japan support is disabled: the JP country plugin still needs a Japan-specific accounting method. "
    "The previous FIFO wiring was an incorrect placeholder and must not be used for Japanese tax results."
)


# JP-specific class
class JP(AbstractCountry):
    def __init__(self) -> None:
        super().__init__("jp", "jpy")

    # Measured in days
    def get_long_term_capital_gain_period(self) -> int:
        # No long-term capital gains in Japan for crypto assets (as of 7/2022)
        return sys.maxsize

    # Default accounting method to use if the user doesn't specify one on the command line
    def get_default_accounting_method(self) -> str:
        return ""

    # Set of accounting methods accepted in the country
    def get_accounting_methods(self) -> Set[str]:
        return set()

    def get_report_generators(self) -> Set[str]:
        return set()

    # Default language to use at report generation if the user doesn't specify it on the command line (in ISO 639-1 format)
    def get_default_generation_language(self) -> str:
        return "ja"


# JP-specific entry point
def rp2_entry() -> None:
    raise SystemExit(_UNSUPPORTED_ACCOUNTING_MESSAGE)
