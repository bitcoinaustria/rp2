# Copyright 2021 eprbell
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
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

LOG_FILE: str = f"./log/rp2_{datetime.now().strftime('%Y_%m_%d_%H_%M_%S_%f')}.log"
_LOG_DIR: Path = Path("./log")
_REGISTERED_LOGGERS: Dict[str, logging.Logger] = {}
_LOGGING_CONFIGURED: bool = False
_CONSOLE_HANDLER: str = "console"
_FILE_HANDLER: str = "file"
_NULL_HANDLER: str = "null"
_HANDLER_KIND_ATTR: str = "_rp2_handler_kind"


def create_logger(logger_name: str = "rp2") -> logging.Logger:
    logger: logging.Logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    _REGISTERED_LOGGERS[logger_name] = logger

    if _LOGGING_CONFIGURED:
        _configure_logger(logger)
    elif not _has_rp2_handler(logger, _NULL_HANDLER):
        null_handler = logging.NullHandler()
        setattr(null_handler, _HANDLER_KIND_ATTR, _NULL_HANDLER)
        logger.addHandler(null_handler)

    return logger


def configure_logging() -> None:
    global _LOGGING_CONFIGURED  # pylint: disable=global-statement

    if _LOGGING_CONFIGURED:
        return

    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    _LOGGING_CONFIGURED = True
    for logger in _REGISTERED_LOGGERS.values():
        _configure_logger(logger)


def _configure_logger(logger: logging.Logger) -> None:
    for handler in logger.handlers[:]:
        if getattr(handler, _HANDLER_KIND_ATTR, None) == _NULL_HANDLER:
            logger.removeHandler(handler)

    if not _has_rp2_handler(logger, _CONSOLE_HANDLER):
        console_handler: logging.StreamHandler = logging.StreamHandler()
        console_format: logging.Formatter = logging.Formatter("%(levelname)s: %(message)s")
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(console_format)
        setattr(console_handler, _HANDLER_KIND_ATTR, _CONSOLE_HANDLER)
        logger.addHandler(console_handler)

    if not _has_rp2_handler(logger, _FILE_HANDLER):
        file_handler: logging.FileHandler = logging.FileHandler(LOG_FILE)
        file_format: logging.Formatter = logging.Formatter("%(asctime)s/%(name)s/%(levelname)s: %(message)s")
        log_level: Optional[str] = os.environ.get("LOG_LEVEL")
        log_level = "INFO" if not log_level else log_level
        file_handler.setLevel(log_level)
        file_handler.setFormatter(file_format)
        setattr(file_handler, _HANDLER_KIND_ATTR, _FILE_HANDLER)
        logger.addHandler(file_handler)


def _has_rp2_handler(logger: logging.Logger, handler_kind: str) -> bool:
    return any(getattr(handler, _HANDLER_KIND_ATTR, None) == handler_kind for handler in logger.handlers)


LOGGER: logging.Logger = create_logger()
