"""
Modular Trading Bot - Supports multiple exchanges
"""

import os
import time
import asyncio
import traceback
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from exchanges import ExchangeFactory
from helpers import TradingLogger
from helpers.lark_bot import LarkBot


@dataclass
class TradingConfig:
    """Configuration class for trading parameters."""
    ticker: str
    contract_id: str
    quantity: Decimal
    take_profit: Decimal
    tick_size: Decimal
    direction: str
    max_orders: int
    wait_time: int
    exchange: str
    grid_step: Decimal
    stop_price: Decimal
    pause_price: Decimal
    aster_boost: bool
    # Stop-loss and take-profit thresholds (percent). Example: 0.08 means 0.08%
    stop_loss_threshold: Decimal = Decimal('0.08')
    take_profit_threshold: Decimal = Decimal('0.12')
    # Whether to use a slightly more aggressive maker price (half-tick toward market)
    maker_aggressive: bool = True
    # Global (wide-range) stop-loss / take-profit in percent (e.g. 5 means 5%)
    global_stop_loss_percent: Decimal = Decimal('5.0')
    global_take_profit_percent: Decimal = Decimal('10.0')

    @property
    def close_order_side(self) -> str:
        """Get the close order side based on bot direction."""
        return 'buy' if self.direction == "sell" else 'sell'


@dataclass
class OrderMonitor:
    """Thread-safe order monitoring state."""
    order_id: Optional[str] = None
    filled: bool = False
    filled_price: Optional[Decimal] = None
    filled_qty: Decimal = 0.0

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
        self.logger = TradingLogger(config.exchange, config.ticker, log_to_console=True)

        # Create exchange client
        try:
            self.exchange_client = ExchangeFactory.create_exchange(
                config.exchange,
                config
            )
        except ValueError as e:
            raise ValueError(f"Failed to create exchange client: {e}")

        # Trading state
        self.active_close_orders = []
        self.last_close_orders = 0
        self.last_open_order_time = 0
        self.last_log_time = 0
        self.current_order_status = None
        # Last filled open order price (used for SL/TP checks)
        self.last_filled_price = None
        self.order_filled_event = asyncio.Event()
        self.order_canceled_event = asyncio.Event()
        self.shutdown_requested = False
        self.loop = None

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
                order_type = message.get('order_type', '')
                filled_size = Decimal(message.get('filled_size'))
                if order_type == "OPEN":
                    self.current_order_status = status

                if status == 'FILLED':
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        try:
                            # try to capture filled price when provided
                            self.last_filled_price = Decimal(message.get('price'))
                        except Exception:
                            # ignore if price is missing or invalid
                            pass
                        # Ensure thread-safe interaction with asyncio event loop
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_filled_event.set)
                        else:
                            # Fallback (should not happen after run() starts)
                            self.order_filled_event.set()

                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")
                    self.logger.log_transaction(order_id, side, message.get('size'), message.get('price'), status)
                elif status == "CANCELED":
                    if order_type == "OPEN":
                        self.order_filled_amount = filled_size
                        if self.loop is not None:
                            self.loop.call_soon_threadsafe(self.order_canceled_event.set)
                        else:
                            self.order_canceled_event.set()

                        if self.order_filled_amount > 0:
                            self.logger.log_transaction(order_id, side, self.order_filled_amount, message.get('price'), status)

                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")
                elif status == "PARTIALLY_FILLED":
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{filled_size} @ {message.get('price')}", "INFO")
                else:
                    self.logger.log(f"[{order_type}] [{order_id}] {status} "
                                    f"{message.get('size')} @ {message.get('price')}", "INFO")

            except Exception as e:
                self.logger.log(f"Error handling order update: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

        # Setup order update handler
        self.exchange_client.setup_order_update_handler(order_update_handler)

    def _calculate_wait_time(self) -> Decimal:
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
            cool_down_time = self.config.wait_time / 4

        # if the program detects active_close_orders during startup, it is necessary to consider cooldown_time
        if self.last_open_order_time == 0 and len(self.active_close_orders) > 0:
            self.last_open_order_time = time.time()

        if time.time() - self.last_open_order_time > cool_down_time:
            return 0
        else:
            return 1

    async def _place_and_monitor_open_order(self) -> bool:
        """Place an order and monitor its execution."""
        try:
            # Reset state before placing order
            self.order_filled_event.clear()
            self.current_order_status = 'OPEN'
            self.order_filled_amount = 0.0

            # Place the order
            order_result = await self.exchange_client.place_open_order(
                self.config.contract_id,
                self.config.quantity,
                self.config.direction
            )

            if not order_result.success:
                self.logger.log(f"Failed to place order: {order_result.error_message}", "ERROR")
                return False

            if order_result.status == 'FILLED':
                return await self._handle_order_result(order_result)
            elif not self.order_filled_event.is_set():
                try:
                    await asyncio.wait_for(self.order_filled_event.wait(), timeout=10)
                except asyncio.TimeoutError:
                    pass

            # Handle order result
            return await self._handle_order_result(order_result)
        except asyncio.CancelledError:
            # Task was cancelled (e.g. Ctrl+C). Attempt graceful shutdown and stop placing new orders.
            self.logger.log("Order placement cancelled (task cancelled). Initiating graceful shutdown.", "WARNING")
            try:
                await self.graceful_shutdown("User interruption (task cancelled)")
            except Exception:
                pass
            return False
        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return False

    async def _handle_order_result(self, order_result) -> bool:
        """Handle the result of an order placement."""
        order_id = order_result.order_id
        filled_price = order_result.price

        if self.order_filled_event.is_set() or order_result.status == 'FILLED':
            # record filled price for P&L/SL/TP checks
            try:
                if filled_price is not None:
                    self.last_filled_price = Decimal(filled_price)
            except Exception:
                pass
            if self.config.aster_boost:
                close_order_result = await self.exchange_client.place_market_order(
                    self.config.contract_id,
                    self.config.quantity,
                    self.config.close_order_side
                )
            else:
                self.last_open_order_time = time.time()
                # Place close order
                close_side = self.config.close_order_side
                if close_side == 'sell':
                    close_price = filled_price * (1 + self.config.take_profit/100)
                else:
                    close_price = filled_price * (1 - self.config.take_profit/100)

                close_order_result = await self.exchange_client.place_close_order(
                    self.config.contract_id,
                    self.config.quantity,
                    close_price,
                    close_side
                )

                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

                return True

        else:
            self.order_canceled_event.clear()
            # Cancel the order if it's still open
            self.logger.log(f"[OPEN] [{order_id}] Cancelling order and placing a new order", "INFO")
            try:
                cancel_result = await self.exchange_client.cancel_order(order_id)
                if not cancel_result.success:
                    self.order_canceled_event.set()
                    self.logger.log(f"[CLOSE] Failed to cancel order {order_id}: {cancel_result.error_message}", "ERROR")
                else:
                    self.current_order_status = "CANCELED"

            except Exception as e:
                self.order_canceled_event.set()
                self.logger.log(f"[CLOSE] Error canceling order {order_id}: {e}", "ERROR")

            if self.config.exchange == "backpack":
                self.order_filled_amount = cancel_result.filled_size
            else:
                # Wait for cancel event or timeout
                if not self.order_canceled_event.is_set():
                    try:
                        await asyncio.wait_for(self.order_canceled_event.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        order_info = await self.exchange_client.get_order_info(order_id)
                        self.order_filled_amount = order_info.filled_size

            if self.order_filled_amount > 0:
                close_side = self.config.close_order_side
                if self.config.aster_boost:
                    close_order_result = await self.exchange_client.place_close_order(
                        self.config.contract_id,
                        self.order_filled_amount,
                        close_side
                    )
                else:
                    if close_side == 'sell':
                        close_price = filled_price * (1 + self.config.take_profit/100)
                    else:
                        close_price = filled_price * (1 - self.config.take_profit/100)

                    close_order_result = await self.exchange_client.place_close_order(
                        self.config.contract_id,
                        self.order_filled_amount,
                        close_price,
                        close_side
                    )
                self.last_open_order_time = time.time()

                if not close_order_result.success:
                    self.logger.log(f"[CLOSE] Failed to place close order: {close_order_result.error_message}", "ERROR")

            return True

        return False

    async def _clear_existing_position(self):
        """Clear any existing position using market orders."""
        position_amt = await self.exchange_client.get_account_positions()
        if position_amt > 0:
            self.logger.log(f"Found existing position of {position_amt}, attempting to close with market order", "INFO")
            # Determine the side for closing the position
            close_side = self.config.close_order_side
            try:
                # Place market order to close position
                close_result = await self.exchange_client.place_market_order(
                    self.config.contract_id,
                    position_amt,
                    close_side
                )
                if close_result.success:
                    self.logger.log(f"Successfully closed existing position with market order", "INFO")
                    # Wait a moment for the position to be updated
                    await asyncio.sleep(2)
                    return True
                else:
                    self.logger.log(f"Failed to close existing position: {close_result.error_message}", "ERROR")
                    return False
            except Exception as e:
                self.logger.log(f"Error while closing existing position: {e}", "ERROR")
                return False
        return True

    async def _log_status_periodically(self):
        """Log status information periodically, including positions."""
        if time.time() - self.last_log_time > 30 or self.last_log_time == 0:
            print("--------------------------------")
            # Check if we have recently filled orders from websocket updates
            recently_filled = False
            if hasattr(self.exchange_client, 'ws_manager') and hasattr(self.exchange_client.ws_manager, 'last_order_update'):
                last_update = self.exchange_client.ws_manager.last_order_update
                if last_update and time.time() - last_update.get('timestamp', 0) < 60:
                    if last_update.get('status') == 'FILLED':
                        recently_filled = True
                        self.logger.log(f"Detected recent fill (order {last_update.get('order_id')})", "INFO")

    async def _meet_grid_step_condition(self) -> bool:
        if self.active_close_orders:
            picker = min if self.config.direction == "buy" else max
            next_close_order = picker(self.active_close_orders, key=lambda o: o["price"])
            next_close_price = next_close_order["price"]

            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
            if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                raise ValueError("No bid/ask data available")

            if self.config.direction == "buy":
                new_order_close_price = best_ask * (1 + self.config.take_profit/100)
                if next_close_price / new_order_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            elif self.config.direction == "sell":
                new_order_close_price = best_bid * (1 - self.config.take_profit/100)
                if new_order_close_price / next_close_price > 1 + self.config.grid_step/100:
                    return True
                else:
                    return False
            else:
                raise ValueError(f"Invalid direction: {self.config.direction}")
        else:
            return True

    async def _check_price_condition(self) -> bool:
        stop_trading = False
        pause_trading = False

        if self.config.pause_price == self.config.stop_price == -1:
            return stop_trading, pause_trading

        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
            raise ValueError("No bid/ask data available")

        if self.config.stop_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.stop_price:
                    stop_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.stop_price:
                    stop_trading = True

        if self.config.pause_price != -1:
            if self.config.direction == "buy":
                if best_ask >= self.config.pause_price:
                    pause_trading = True
            elif self.config.direction == "sell":
                if best_bid <= self.config.pause_price:
                    pause_trading = True

        return stop_trading, pause_trading

    async def _lark_bot_notify(self, message: str):
        lark_token = os.getenv("LARK_TOKEN")
        if lark_token:
            async with LarkBot(lark_token) as bot:
                await bot.send_text(message)

    async def run(self):
        """Main trading loop."""
        try:
            self.config.contract_id, self.config.tick_size = await self.exchange_client.get_contract_attributes()

            # Log current TradingConfig
            self.logger.log("=== Trading Configuration ===", "INFO")
            self.logger.log(f"Ticker: {self.config.ticker}", "INFO")
            self.logger.log(f"Contract ID: {self.config.contract_id}", "INFO")
            self.logger.log(f"Quantity: {self.config.quantity}", "INFO")
            self.logger.log(f"Take Profit: {self.config.take_profit}%", "INFO")
            self.logger.log(f"Direction: {self.config.direction}", "INFO")
            self.logger.log(f"Max Orders: {self.config.max_orders}", "INFO")
            self.logger.log(f"Wait Time: {self.config.wait_time}s", "INFO")
            self.logger.log(f"Exchange: {self.config.exchange}", "INFO")
            self.logger.log(f"Grid Step: {self.config.grid_step}%", "INFO")
            self.logger.log(f"Stop Price: {self.config.stop_price}", "INFO")
            self.logger.log(f"Pause Price: {self.config.pause_price}", "INFO")
            self.logger.log(f"Aster Boost: {self.config.aster_boost}", "INFO")
            self.logger.log("=============================", "INFO")

            # Capture the running event loop for thread-safe callbacks
            self.loop = asyncio.get_running_loop()
            # Connect to exchange
            await self.exchange_client.connect()

            # Main trading loop
            while not self.shutdown_requested:
                # Get positions first
                position_amt = await self.exchange_client.get_account_positions()
                
                # If we have a position but no active orders, clear it first
                if position_amt > 0:
                    # Check unrealized P&L against thresholds and close immediately if triggered
                    try:
                        if self.last_filled_price is not None and self.last_filled_price > 0:
                            best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
                            if best_bid > 0 and best_ask > 0:
                                mark_price = (best_bid + best_ask) / 2
                                entry_price = Decimal(self.last_filled_price)
                                if self.config.direction == 'buy':
                                    profit_pct = (Decimal(mark_price) - entry_price) / entry_price * 100
                                else:
                                    profit_pct = (entry_price - Decimal(mark_price)) / entry_price * 100

                                # Stop-loss: loss >= threshold -> close
                                if profit_pct <= -abs(self.config.stop_loss_threshold):
                                    self.logger.log(f"Position loss {profit_pct:.4f}% <= -{self.config.stop_loss_threshold}%, closing at market", "WARNING")
                                    await self.exchange_client.place_market_order(self.config.contract_id, position_amt, self.config.close_order_side)
                                    # after market close, reset last_filled_price to avoid duplicate actions
                                    self.last_filled_price = None
                                    # re-evaluate positions after a short delay
                                    await asyncio.sleep(1)
                                    position_amt = await self.exchange_client.get_account_positions()
                                # Take-profit: profit >= threshold -> close
                                elif profit_pct >= abs(self.config.take_profit_threshold):
                                    self.logger.log(f"Position profit {profit_pct:.4f}% >= {self.config.take_profit_threshold}%, closing at market", "INFO")
                                    await self.exchange_client.place_market_order(self.config.contract_id, position_amt, self.config.close_order_side)
                                    self.last_filled_price = None
                                    await asyncio.sleep(1)
                                    position_amt = await self.exchange_client.get_account_positions()
                    except Exception as e:
                        self.logger.log(f"Error checking SL/TP conditions: {e}", "ERROR")

                    await self._clear_existing_position()
                    # Recheck position after clearing
                    position_amt = await self.exchange_client.get_account_positions()
                    if position_amt > 0:
                        self.logger.log(f"Warning: Position {position_amt} still exists after clearing attempt", "WARNING")

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

                # SL/TP check: if we have an open position and a recorded fill price,
                # evaluate unrealized P&L and close at market if thresholds are hit.
                if position_amt > 0 and self.last_filled_price is not None:
                    try:
                        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
                        # use mid price as mark
                        mark_price = (best_bid + best_ask) / Decimal(2)
                        last_price = Decimal(self.last_filled_price)

                        if last_price > 0:
                            if self.config.direction == 'buy':
                                profit_frac = (mark_price - last_price) / last_price
                            else:
                                profit_frac = (last_price - mark_price) / last_price

                            stop_frac = (self.config.stop_loss_threshold or Decimal(0)) / Decimal(100)
                            tp_frac = (self.config.take_profit_threshold or Decimal(0)) / Decimal(100)

                            if profit_frac <= -stop_frac:
                                # Stop-loss: close position at market
                                self.logger.log(f"Position loss {profit_frac:.6f} <= -{stop_frac:.6f}, executing market close", "WARNING")
                                close_side = self.config.close_order_side
                                try:
                                    res = await self.exchange_client.place_market_order(self.config.contract_id, position_amt, close_side)
                                    if res.success:
                                        self.logger.log(f"Closed position {position_amt} at market for SL", "INFO")
                                        # clear last filled price so we don't repeatedly trigger
                                        self.last_filled_price = None
                                        # give markets/positions a moment to update
                                        await asyncio.sleep(1)
                                        position_amt = await self.exchange_client.get_account_positions()
                                    else:
                                        self.logger.log(f"Failed to close position for SL: {res.error_message}", "ERROR")
                                except Exception as e:
                                    self.logger.log(f"Error executing market close for SL: {e}", "ERROR")

                            elif profit_frac >= tp_frac:
                                # Take-profit: close position at market
                                self.logger.log(f"Position profit {profit_frac:.6f} >= {tp_frac:.6f}, executing market close", "INFO")
                                close_side = self.config.close_order_side
                                try:
                                    res = await self.exchange_client.place_market_order(self.config.contract_id, position_amt, close_side)
                                    if res.success:
                                        self.logger.log(f"Closed position {position_amt} at market for TP", "INFO")
                                        self.last_filled_price = None
                                        await asyncio.sleep(1)
                                        position_amt = await self.exchange_client.get_account_positions()
                                    else:
                                        self.logger.log(f"Failed to close position for TP: {res.error_message}", "ERROR")
                                except Exception as e:
                                    self.logger.log(f"Error executing market close for TP: {e}", "ERROR")
                    except Exception as e:
                        self.logger.log(f"Error checking SL/TP: {e}", "ERROR")

                # Global wide-range SL/TP check (超大区间止损/止盈)
                # If the global thresholds are hit, close position at market and shut down the bot.
                try:
                    if position_amt > 0 and self.last_filled_price is not None:
                        best_bid, best_ask = await self.exchange_client.fetch_bbo_prices(self.config.contract_id)
                        if best_bid > 0 and best_ask > 0:
                            mark_price = (best_bid + best_ask) / Decimal(2)
                            entry_price = Decimal(self.last_filled_price)
                            if self.config.direction == 'buy':
                                global_profit_frac = (mark_price - entry_price) / entry_price
                            else:
                                global_profit_frac = (entry_price - mark_price) / entry_price

                            global_sl_frac = (self.config.global_stop_loss_percent or Decimal(0)) / Decimal(100)
                            global_tp_frac = (self.config.global_take_profit_percent or Decimal(0)) / Decimal(100)

                            if global_profit_frac <= -global_sl_frac:
                                msg = (f"GLOBAL STOP-LOSS TRIGGERED: profit {global_profit_frac:.6f} <= -{global_sl_frac:.6f}. "
                                       f"Closing position {position_amt} at market and shutting down.")
                                self.logger.log(msg, "ERROR")
                                # close at market
                                try:
                                    res = await self.exchange_client.place_market_order(self.config.contract_id, position_amt, self.config.close_order_side)
                                    if res.success:
                                        self.logger.log(f"Global SL: closed position {position_amt} at market", "INFO")
                                    else:
                                        self.logger.log(f"Global SL: failed to close position: {res.error_message}", "ERROR")
                                except Exception as e:
                                    self.logger.log(f"Global SL: error during market close: {e}", "ERROR")

                                # notify and shutdown
                                await self._lark_bot_notify(msg)
                                self.shutdown_requested = True
                                # continue to outer loop to exit gracefully
                                continue

                            if global_profit_frac >= global_tp_frac:
                                msg = (f"GLOBAL TAKE-PROFIT TRIGGERED: profit {global_profit_frac:.6f} >= {global_tp_frac:.6f}. "
                                       f"Closing position {position_amt} at market and shutting down.")
                                self.logger.log(msg, "INFO")
                                try:
                                    res = await self.exchange_client.place_market_order(self.config.contract_id, position_amt, self.config.close_order_side)
                                    if res.success:
                                        self.logger.log(f"Global TP: closed position {position_amt} at market", "INFO")
                                    else:
                                        self.logger.log(f"Global TP: failed to close position: {res.error_message}", "ERROR")
                                except Exception as e:
                                    self.logger.log(f"Global TP: error during market close: {e}", "ERROR")

                                await self._lark_bot_notify(msg)
                                self.shutdown_requested = True
                                continue
                except Exception as e:
                    self.logger.log(f"Error during global SL/TP check: {e}", "ERROR")

                # Periodic logging
                mismatch_detected = await self._log_status_periodically()

                stop_trading, pause_trading = await self._check_price_condition()
                if stop_trading:
                    msg = f"\n\nWARNING: [{self.config.exchange.upper()}_{self.config.ticker.upper()}] \n"
                    msg += "Stopped trading due to stop price\n"
                    await self.graceful_shutdown(msg)
                    await self._lark_bot_notify(msg.lstrip())
                    continue

                if pause_trading:
                    await asyncio.sleep(5)
                    continue

                if not mismatch_detected:
                    wait_time = self._calculate_wait_time()

                    if wait_time > 0:
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        meet_grid_step_condition = await self._meet_grid_step_condition()
                        if not meet_grid_step_condition:
                            await asyncio.sleep(1)
                            continue

                        await self._place_and_monitor_open_order()
                        self.last_close_orders += 1

        except KeyboardInterrupt:
            self.logger.log("Bot stopped by user")
            await self.graceful_shutdown("User interruption (Ctrl+C)")
        except Exception as e:
            self.logger.log(f"Critical error: {e}", "ERROR")
            await self.graceful_shutdown(f"Critical error: {e}")
            raise
        finally:
            # Ensure all connections are closed even if graceful shutdown fails
            try:
                await self.exchange_client.disconnect()
            except Exception as e:
                self.logger.log(f"Error disconnecting from exchange: {e}", "ERROR")
