"""
Base exchange client interface.
All exchange implementations should inherit from this class.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from dataclasses import dataclass


@dataclass
class OrderResult:
    """Standardized order result structure."""
    success: bool
    order_id: Optional[str] = None
    side: Optional[str] = None
    size: Optional[float] = None
    price: Optional[float] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    filled_size: Optional[float] = None


@dataclass
class OrderInfo:
    """Standardized order information structure."""
    order_id: str
    side: str
    size: float
    price: float
    status: str
    filled_size: float = 0.0
    remaining_size: float = 0.0


class BaseExchangeClient(ABC):
    """Base class for all exchange clients."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize the exchange client with configuration."""
        self.config = config
        self._validate_config()

    @abstractmethod
    def _validate_config(self) -> None:
        """Validate the exchange-specific configuration."""
        pass

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the exchange (WebSocket, etc.)."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the exchange."""
        pass

    @abstractmethod
    async def place_open_order(self, contract_id: str, quantity: float, direction: str) -> OrderResult:
        """Place an open order."""
        pass

    @abstractmethod
    async def place_close_order(self, contract_id: str, quantity: float, price: float, side: str) -> OrderResult:
        """Place a close order."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order."""
        pass

    @abstractmethod
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information."""
        pass

    @abstractmethod
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract."""
        pass

    @abstractmethod
    async def get_account_positions(self) -> float:
        """Get account positions."""
        pass

    @abstractmethod
    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        pass

    @abstractmethod
    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        pass
