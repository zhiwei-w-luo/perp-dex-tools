"""
Modular Trading Bot - Supports multiple exchanges
"""

import time
import asyncio
import traceback
from dataclasses import dataclass
from typing import Optional
import dotenv

from exchanges import ExchangeFactory
from helpers import TradingLogger

dotenv.load_dotenv()


@dataclass
class TradingConfig:
    """Configuration class for trading parameters."""
    contract_id: str
    quantity: float
    take_profit: float
    direction: str
    max_orders: int
    wait_time: int
    exchange: str

    @property
    def close_order_side(self) -> str:
        """Get the close order side based on bot direction."""
        return 'buy' if self.direction == "sell" else 'sell'


@dataclass
class OrderMonitor:
    """Thread-safe order monitoring state."""
    order_id: Optional[str] = None
    filled: bool = False
    filled_price: Optional[float] = None
    filled_qty: float = 0.0

    def reset(self):
        """Reset the monitor state."""
        self.order_id = None
        self.filled = False
        self.filled_price = None
        self.filled_qty = 0.0


class TradingBot:
    """Modular Trading Bot - Main trading logic supporting multiple exchanges."""

    def __init__(self, config: TradingConfig):
        self.config = config
        self.logger = TradingLogger(config.contract_id, log_to_console=True)

        # Create exchange client
        try:
            self.exchange_client = ExchangeFactory.create_exchange(
                config.exchange,
                {'contract_id': config.contract_id}
            )
        except ValueError as e:
            raise ValueError(f"Failed to create exchange client: {e}")

        # Trading state
        self.active_close_orders = []
        self.last_close_orders = 0
        self.last_open_order_time = 0
        self.last_log_time = 0
        self.current_order_status = "PENDING"
        self.order_filled_event = asyncio.Event()
        self.shutdown_requested = False

        # Register order callback
        self._setup_websocket_handlers()

    async def graceful_shutdown(self, reason: str = "Unknown"):
        """Perform graceful shutdown of the trading bot."""
        self.logger.log(f"Starting graceful shutdown: {reason}", "INFO")
        self.shutdown_requested = True

        try:
            # Disconnect from exchange
            await self.exchange_client.disconnect()
            self.logger.log("Graceful shutdown completed", "INFO")

        except Exception as e:
            self.logger.log(f"Error during graceful shutdown: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    def _setup_websocket_handlers(self):
        """Setup WebSocket handlers for order updates."""
        def order_update_handler(message):
            """Handle order updates from WebSocket."""
            try:
                # Check if this is for our contract
                if message.get('contract_id') != self.config.contract_id:
                    return

                order_id = message.get('order_id')
                status = message.get('status')
                side = message.get('side', '')

                if side == self.config.close_order_side:
                    order_type = "CLOSE"
                else:
                    order_type = "OPEN"

                if status == 'FILLED':
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")

                    # Log the filled transaction to CSV using log_transaction function
                    self.logger.log_transaction(
                        order_id=order_id,
                        side=side,
                        quantity=float(message.get('size', 0)),
                        price=float(message.get('price', 0)),
                        status=status
                    )

                    self.order_filled_event.set()
                else:
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")

            except Exception as e:
                self.logger.log(f"Error handling order update: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

        # Setup order update handler
        self.exchange_client.setup_order_update_handler(order_update_handler)

    def _calculate_wait_time(self) -> float:
        """Calculate wait time between orders."""
        cool_down_time = self.config.wait_time

        if len(self.active_close_orders) < self.last_close_orders:
            self.last_close_orders = len(self.active_close_orders)
            return 0

        self.last_close_orders = len(self.active_close_orders)
        if len(self.active_close_orders) >= self.config.max_orders:
            return 1

        if len(self.active_close_orders) / self.config.max_orders >= 2/3:
            cool_down_time = 2 * self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/3:
            cool_down_time = self.config.wait_time
        elif len(self.active_close_orders) / self.config.max_orders >= 1/6:
            cool_down_time = self.config.wait_time / 2
        else:
            cool_down_time = 60

        if time.time() - self.last_open_order_time > cool_down_time:
            return 0
        else:
            return 1

    async def _place_and_monitor_open_order(self) -> bool:
        """Place an order and monitor its execution."""
        try:
            # Reset state before placing order
            self.order_filled_event.clear()

            # Place the order
            order_result = await self.exchange_client.place_open_order(
                self.config.contract_id,
                self.config.quantity,
                self.config.direction
            )

            if not order_result.success:
                self.logger.log(f"Failed to place order: {order_result.error_message}", "ERROR")
                return False

            self.last_open_order_time = time.time()

            # Wait for fill or timeout
            if order_result.status != 'FILLED':
                try:
                    await asyncio.wait_for(self.order_filled_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

            # Handle order result
            return await self._handle_order_result(order_result)

        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            return False

    async def _handle_order_result(self, order_result) -> bool:
        """Handle the result of an order placement."""
        order_id = order_result.order_id

        # Get current order status
        order_info = await self.exchange_client.get_order_info(order_id)

        if not order_info:
            self.logger.log(f"Could not get order info for {order_id}", "WARNING")
            return False

        if order_info.status == 'FILLED':
            self.current_order_status = "FILLED"

            # Place close order
            filled_price = order_info.price
            if filled_price > 0:
                close_side = self.config.close_order_side
                if close_side == 'sell':
                    close_price = filled_price + self.config.take_profit
                else:
                    close_price = filled_price - self.config.take_profit

                self.logger.log(f"[OPEN] [{order_id}] Order placed and FILLED: "
                                f"{self.config.quantity} @ {filled_price}", "INFO")

                close_order_result = await self.exchange_client.place_close_order(
                    self.config.contract_id,
                    self.config.quantity,
                    close_price,
                    close_side
                )

                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

            return True

        elif order_info.status in ['OPEN', 'PARTIALLY_FILLED']:
            # Cancel the order if it's still open
            try:
                cancel_result = await self.exchange_client.cancel_order(order_id)
                if not cancel_result.success:
                    self.logger.log(f"[CLOSE] Failed to cancel order {order_id}: {cancel_result.error_message}", "ERROR")
                else:
                    self.current_order_status = "CANCELED"

            except Exception as e:
                self.logger.log(f"[CLOSE] Error canceling order {order_id}: {e}", "ERROR")

            self.logger.log(f"[OPEN] [{order_id}] Order placed and PARTIALLY FILLED: "
                            f"{order_info.filled_size} @ {order_info.price}", "INFO")

            if order_info.filled_size > 0:
                close_side = self.config.close_order_side
                if close_side == 'sell':
                    close_price = order_info.price + self.config.take_profit
                else:
                    close_price = order_info.price - self.config.take_profit

                close_order_result = await self.exchange_client.place_close_order(
                    self.config.contract_id,
                    order_info.filled_size,
                    close_price,
                    close_side
                )

                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

            return True

        return False

    async def _log_status_periodically(self):
        """Log status information periodically, including positions."""
        if time.time() - self.last_log_time > 60 or self.last_log_time == 0:
            print("--------------------------------")
            try:
                # Get active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Get positions
                positions_data = await self.exchange_client.get_account_positions()

                if not positions_data or 'data' not in positions_data:
                    self.logger.log("Failed to get positions", "WARNING")
                    position_amt = 0
                else:
                    # The API returns positions under data.positionList
                    positions = positions_data.get('data', {}).get('positionList', [])
                    if positions:
                        # Find position for current contract
                        position = None
                        for p in positions:
                            if isinstance(p, dict) and p.get('contractId') == self.config.contract_id:
                                position = p
                                break

                        if position:
                            position_amt = abs(float(position.get('openSize', 0)))
                        else:
                            position_amt = 0
                    else:
                        position_amt = 0

                # Calculate active closing amount
                active_close_amount = sum(
                    float(order.get('size', 0))
                    for order in self.active_close_orders
                    if isinstance(order, dict)
                )

                self.logger.log(f"Current Position: {position_amt} | Active closing amount: {active_close_amount}")

                # Check for position mismatch
                if abs(position_amt - active_close_amount) > (2 * self.config.quantity):
                    self.logger.log("ERROR: Position mismatch detected", "ERROR")
                    self.logger.log("###### ERROR ###### ERROR ###### ERROR ###### ERROR #####\n", "ERROR")
                    self.logger.log("Please manually rebalance your position and take-profit orders", "ERROR")
                    self.logger.log("请手动平衡当前仓位和正在关闭的仓位", "ERROR")
                    self.logger.log(
                        f"current position: {position_amt} | active closing amount: {active_close_amount}\n", "ERROR")
                    self.logger.log("###### ERROR ###### ERROR ###### ERROR ###### ERROR #####", "ERROR")
                    if not self.shutdown_requested:
                        self.shutdown_requested = True
                    return

            except Exception as e:
                self.logger.log(f"Error in periodic status check: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

            self.last_log_time = time.time()
            print("--------------------------------")

    async def run(self):
        """Main trading loop."""
        try:
            # Connect to exchange
            await self.exchange_client.connect()

            # Main trading loop
            while not self.shutdown_requested:
                # Update active orders
                active_orders = await self.exchange_client.get_active_orders(self.config.contract_id)

                # Filter close orders
                self.active_close_orders = []
                for order in active_orders:
                    if order.side == self.config.close_order_side:
                        self.active_close_orders.append({
                            'id': order.order_id,
                            'price': order.price,
                            'size': order.size
                        })

                # Periodic logging
                await self._log_status_periodically()
                wait_time = self._calculate_wait_time()

                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    await self._place_and_monitor_open_order()
                    self.last_close_orders += 1

        except KeyboardInterrupt:
            self.logger.log("Bot stopped by user")
            await self.graceful_shutdown("User interruption (Ctrl+C)")
        except Exception as e:
            self.logger.log(f"Critical error: {e}", "ERROR")
            self.logger.log(traceback.format_exc(), "ERROR")
            await self.graceful_shutdown(f"Critical error: {e}")
            raise
        finally:
            # Ensure all connections are closed even if graceful shutdown fails
            try:
                await self.exchange_client.disconnect()
            except Exception as e:
                self.logger.log(f"Error disconnecting from exchange: {e}", "ERROR")
