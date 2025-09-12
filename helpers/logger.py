"""
Trading logger with structured output and error handling.
"""

import os
import csv
import logging
from datetime import datetime
import pytz


class TradingLogger:
    """Enhanced logging with structured output and error handling."""

    def __init__(self, contract_id: str, log_to_console: bool = False):
        self.contract_id = contract_id
        self.log_file = f"{contract_id}_transactions_log.csv"
        self.debug_log_file = f"{contract_id}_bot_activity.log"
        self.timezone = pytz.timezone(os.getenv('TIMEZONE', 'Asia/Shanghai'))
        self.logger = self._setup_logger(log_to_console)

    def _setup_logger(self, log_to_console: bool) -> logging.Logger:
        """Setup the logger with proper configuration."""
        logger = logging.getLogger(f"trading_bot_{self.contract_id}")
        logger.setLevel(logging.INFO)

        # Prevent duplicate handlers
        if logger.handlers:
            return logger

        class TimeZoneFormatter(logging.Formatter):
            def __init__(self, fmt=None, datefmt=None, tz=None):
                super().__init__(fmt=fmt, datefmt=datefmt)
                self.tz = tz

            def formatTime(self, record, datefmt=None):
                dt = datetime.fromtimestamp(record.created, tz=self.tz)
                if datefmt:
                    return dt.strftime(datefmt)
                return dt.isoformat()

        formatter = TimeZoneFormatter(
            "%(asctime)s.%(msecs)03d - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            tz=self.timezone
        )

        # File handler
        file_handler = logging.FileHandler(self.debug_log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Console handler if requested
        if log_to_console:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        return logger

    def log(self, message: str, level: str = "INFO"):
        """Log a message with the specified level."""
        if level.upper() == "DEBUG":
            self.logger.debug(message)
        elif level.upper() == "INFO":
            self.logger.info(message)
        elif level.upper() == "WARNING":
            self.logger.warning(message)
        elif level.upper() == "ERROR":
            self.logger.error(message)
        else:
            self.logger.info(message)

    def log_transaction(self, order_id: str, side: str, quantity: float, price: float, status: str):
        """Log a transaction to CSV file."""
        try:
            timestamp = datetime.now(self.timezone).strftime("%Y-%m-%d %H:%M:%S")
            row = [timestamp, order_id, side, quantity, price, status]

            # Check if file exists to write headers
            file_exists = os.path.isfile(self.log_file)

            with open(self.log_file, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                if not file_exists:
                    writer.writerow(['Timestamp', 'OrderID', 'Side', 'Quantity', 'Price', 'Status'])
                writer.writerow(row)

        except Exception as e:
            self.log(f"Failed to log transaction: {e}", "ERROR")
