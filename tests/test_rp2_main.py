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

import os
import unittest
from configparser import ConfigParser
from tempfile import NamedTemporaryFile

from rp2.configuration import Configuration, Keyword
from rp2.plugin.accounting_method.lifo import AccountingMethod as LifoAccountingMethod
from rp2.plugin.country.us import US
from rp2.rp2_main import _resolve_application_method, _resolve_transfer_semantics


class TestRP2Main(unittest.TestCase):
    _country = US()

    @staticmethod
    def _create_configuration(config: ConfigParser) -> Configuration:
        with NamedTemporaryFile("w", delete=False) as temporary_file:
            config.write(temporary_file)
            temporary_file.flush()
            configuration = Configuration(temporary_file.name, TestRP2Main._country)
        os.remove(temporary_file.name)
        return configuration

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


if __name__ == "__main__":
    unittest.main()
