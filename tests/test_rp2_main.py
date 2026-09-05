# Copyright 2026 eprbell
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

import unittest
from configparser import ConfigParser
from pathlib import Path
from tempfile import TemporaryDirectory

from rp2.configuration import Configuration, Keyword
from rp2.plugin.accounting_method.lifo import AccountingMethod as LifoAccountingMethod
from rp2.plugin.country.at import AT
from rp2.plugin.country.us import US
from rp2.rp2_error import RP2ValueError
from rp2.rp2_main import (
    _resolve_application_method,
    _resolve_transfer_semantics,
    _validate_country_computation_application,
)


class TestRP2Main(unittest.TestCase):
    _country = US()

    @staticmethod
    def _create_configuration(config: ConfigParser) -> Configuration:
        with TemporaryDirectory() as temporary_directory:
            configuration_path = Path(temporary_directory) / "configuration.ini"
            with configuration_path.open("w", encoding="utf-8") as configuration_file:
                config.write(configuration_file)
            return Configuration(str(configuration_path), TestRP2Main._country)

    @staticmethod
    def _base_config() -> ConfigParser:
        result = ConfigParser()
        result.read("./config/test_data.ini")
        return result

    def test_transfer_semantics_falls_back_to_earliest_accounting_method(self) -> None:
        configuration = Configuration("./config/test_data.ini", self._country)
        transfer_semantics = _resolve_transfer_semantics(configuration, {2020: "lifo"})
        self.assertIsInstance(transfer_semantics, LifoAccountingMethod)

    def test_mixed_application_methods_are_rejected(self) -> None:
        config = self._base_config()
        config[Keyword.APPLICATION_METHODS.value] = {"2024": "universal", "2025": "per_wallet"}
        configuration = self._create_configuration(config)
        with self.assertRaises(SystemExit):
            _resolve_application_method(configuration)

    def test_mixed_transfer_methods_are_rejected(self) -> None:
        config = self._base_config()
        config[Keyword.TRANSFER_METHODS.value] = {"2024": "fifo", "2025": "lifo"}
        configuration = self._create_configuration(config)
        with self.assertRaises(SystemExit):
            _resolve_transfer_semantics(configuration, {2020: "fifo"})

    def test_per_wallet_rejects_country_wide_computation_hook(self) -> None:
        with self.assertRaisesRegex(RP2ValueError, r"would bypass the country hook"):
            _validate_country_computation_application(AT(), use_per_wallet=True)

    def test_per_wallet_accepts_country_without_computation_hook(self) -> None:
        _validate_country_computation_application(US(), use_per_wallet=True)


if __name__ == "__main__":
    unittest.main()
