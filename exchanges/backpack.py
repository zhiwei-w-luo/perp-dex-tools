"""
Backpack exchange client implementation.
"""

import os
import asyncio
import json
import time
import base64
import sys
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from cryptography.hazmat.primitives.asymmetric import ed25519
import websockets
from bpx.public import Public
from bpx.account import Account
from bpx.constants.enums import OrderTypeEnum, TimeInForceEnum

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger


class BackpackWebSocketManager:
    """WebSocket manager for Backpack order updates."""

    def __init__(self, public_key: str, secret_key: str, symbol: str, order_update_callback):
        self.public_key = public_key
        self.secret_key = secret_key
        self.symbol = symbol
        self.order_update_callback = order_update_callback
        self.websocket = None
        self.running = False
        self.ws_url = "wss://ws.backpack.exchange"
        self.logger = None

        # Initialize ED25519 private key from base64 decoded secret
        self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(secret_key)
        )

    def _generate_signature(self, instruction: str, timestamp: int, window: int = 5000) -> str:
        """Generate ED25519 signature for WebSocket authentication."""
        # Create the message string in the same format as BPX package
        message = f"instruction={instruction}&timestamp={timestamp}&window={window}"

        # Sign the message using ED25519 private key
        signature_bytes = self.private_key.sign(message.encode())

        # Return base64 encoded signature
        return base64.b64encode(signature_bytes).decode()

    async def connect(self):
        """Connect to Backpack WebSocket."""
        try:
            self.websocket = await websockets.connect(self.ws_url)
            self.running = True

            # Subscribe to order updates for the specific symbol
            timestamp = int(time.time() * 1000)
            signature = self._generate_signature("subscribe", timestamp)

            subscribe_message = {
                "method": "SUBSCRIBE",
                "params": [f"account.orderUpdate.{self.symbol}"],
                "signature": [
                    self.public_key,
                    signature,
                    str(timestamp),
                    "5000"
                ]
            }

            await self.websocket.send(json.dumps(subscribe_message))
            if self.logger:
                self.logger.log(f"Subscribed to order updates for {self.symbol}", "INFO")

            # Start listening for messages
            await self._listen()

        except Exception as e:
            if self.logger:
                self.logger.log(f"WebSocket connection error: {e}", "ERROR")
            raise

    async def _listen(self):
        """Listen for WebSocket messages."""
        try:
            async for message in self.websocket:
                if not self.running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    if self.logger:
                        self.logger.log(f"Failed to parse WebSocket message: {e}", "ERROR")
                except Exception as e:
                    if self.logger:
                        self.logger.log(f"Error handling WebSocket message: {e}", "ERROR")

        except websockets.exceptions.ConnectionClosed:
            if self.logger:
                self.logger.log("WebSocket connection closed", "WARNING")
        except Exception as e:
            if self.logger:
                self.logger.log(f"WebSocket listen error: {e}", "ERROR")

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket messages."""
        try:
            stream = data.get('stream', '')
            payload = data.get('data', {})

            if 'orderUpdate' in stream:
                await self._handle_order_update(payload)
            else:
                self.logger.log(f"Unknown WebSocket message: {data}", "ERROR")

        except Exception as e:
            if self.logger:
                self.logger.log(f"Error handling WebSocket message: {e}", "ERROR")

    async def _handle_order_update(self, order_data: Dict[str, Any]):
        """Handle order update messages."""
        try:
            # Call the order update callback if it exists
            if hasattr(self, 'order_update_callback') and self.order_update_callback:
                await self.order_update_callback(order_data)
        except Exception as e:
            if self.logger:
                self.logger.log(f"Error handling order update: {e}", "ERROR")

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            if self.logger:
                self.logger.log("WebSocket disconnected", "INFO")

    def set_logger(self, logger):
        """Set the logger instance."""
        self.logger = logger

    def set_order_filled_event(self, event):
        """Set the order filled event for synchronization."""
        self.order_filled_event = event


class BackpackClient(BaseExchangeClient):
    """Backpack exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Backpack client."""
        super().__init__(config)

        # Backpack credentials from environment
        self.public_key = os.getenv('BACKPACK_PUBLIC_KEY')
        self.secret_key = os.getenv('BACKPACK_SECRET_KEY')

        if not self.public_key or not self.secret_key:
            raise ValueError("BACKPACK_PUBLIC_KEY and BACKPACK_SECRET_KEY must be set in environment variables")

        # Initialize Backpack clients using official SDK
        self.public_client = Public()
        self.account_client = Account(
            public_key=self.public_key,
            secret_key=self.secret_key
        )

        self._order_update_handler = None

    def _validate_config(self) -> None:
        """Validate Backpack configuration."""
        required_env_vars = ['BACKPACK_PUBLIC_KEY', 'BACKPACK_SECRET_KEY']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    async def connect(self) -> None:
        """Connect to Backpack WebSocket."""
        # Initialize WebSocket manager
        self.ws_manager = BackpackWebSocketManager(
            public_key=self.public_key,
            secret_key=self.secret_key,
            symbol=self.config.contract_id,  # Use contract_id as symbol for Backpack
            order_update_callback=self._handle_websocket_order_update
        )
        # Pass config to WebSocket manager for order type determination
        self.ws_manager.config = self.config

        # Initialize logger using the same format as helpers
        self.logger = TradingLogger(exchange="backpack", ticker=self.config.ticker, log_to_console=False)
        self.ws_manager.set_logger(self.logger)

        try:
            # Start WebSocket connection in background task
            asyncio.create_task(self.ws_manager.connect())
            # Wait a moment for connection to establish
            await asyncio.sleep(2)
        except Exception as e:
            self.logger.log(f"Error connecting to Backpack WebSocket: {e}", "ERROR")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Backpack."""
        try:
            if hasattr(self, 'ws_manager') and self.ws_manager:
                await self.ws_manager.disconnect()
        except Exception as e:
            self.logger.log(f"Error during Backpack disconnect: {e}", "ERROR")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "backpack"

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

    async def _handle_websocket_order_update(self, order_data: Dict[str, Any]):
        """Handle order updates from WebSocket."""
        try:
            event_type = order_data.get('e', '')
            order_id = order_data.get('i', '')
            symbol = order_data.get('s', '')
            side = order_data.get('S', '')
            quantity = order_data.get('q', '0')
            price = order_data.get('p', '0')
            fill_quantity = order_data.get('z', '0')
            
            # Store last order update with timestamp for status checking
            self.last_order_update = {
                'order_id': order_id,
                'status': 'FILLED' if (event_type == 'orderFill' and quantity == fill_quantity) else 'PARTIAL',
                'timestamp': time.time(),
                'side': side,
                'quantity': quantity,
                'fill_quantity': fill_quantity
            }

            # Only process orders for our symbol
            if symbol != self.config.contract_id:
                return

            # Determine order side
            if side.upper() == 'BID':
                order_side = 'buy'
            elif side.upper() == 'ASK':
                order_side = 'sell'
            else:
                self.logger.log(f"Unexpected order side: {side}", "ERROR")
                sys.exit(1)

            # Check if this is a close order (opposite side from bot direction)
            is_close_order = (order_side == self.config.close_order_side)
            order_type = "CLOSE" if is_close_order else "OPEN"

            if event_type == 'orderFill' and quantity == fill_quantity:
                if self._order_update_handler:
                    self._order_update_handler({
                        'order_id': order_id,
                        'side': order_side,
                        'order_type': order_type,
                        'status': 'FILLED',
                        'size': quantity,
                        'price': price,
                        'contract_id': symbol,
                        'filled_size': fill_quantity
                    })

            elif event_type in ['orderFill', 'orderAccepted', 'orderCancelled', 'orderExpired']:
                if event_type == 'orderFill':
                    status = 'PARTIALLY_FILLED'
                elif event_type == 'orderAccepted':
                    status = 'OPEN'
                elif event_type in ['orderCancelled', 'orderExpired']:
                    status = 'CANCELED'

                if self._order_update_handler:
                    self._order_update_handler({
                        'order_id': order_id,
                        'side': order_side,
                        'order_type': order_type,
                        'status': status,
                        'size': quantity,
                        'price': price,
                        'contract_id': symbol,
                        'filled_size': fill_quantity
                    })

        except Exception as e:
            self.logger.log(f"Error handling WebSocket order update: {e}", "ERROR")

    @query_retry(default_return=(0, 0))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        # Get order book depth from Backpack
        order_book = self.public_client.get_depth(contract_id)

        # Extract bids and asks directly from Backpack response
        bids = order_book.get('bids', [])
        asks = order_book.get('asks', [])

        # Sort bids and asks
        bids = sorted(bids, key=lambda x: Decimal(x[0]), reverse=True)  # (highest price first)
        asks = sorted(asks, key=lambda x: Decimal(x[0]))                # (lowest price first)

        # Best bid is the highest price someone is willing to buy at
        best_bid = Decimal(bids[0][0]) if bids and len(bids) > 0 else 0
        # Best ask is the lowest price someone is willing to sell at
        best_ask = Decimal(asks[0][0]) if asks and len(asks) > 0 else 0

        return best_bid, best_ask

    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order with Backpack using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 15
        retry_count = 0

        while retry_count < max_retries:
            retry_count += 1

            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message='Invalid bid/ask prices')

            if direction == 'buy':
                # For buy orders, choose price based on maker_aggressive flag
                if getattr(self.config, 'maker_aggressive', True):
                    # slightly below best ask (half-tick toward market)
                    order_price = best_ask - (self.config.tick_size / Decimal(2))
                else:
                    # original, more passive behavior (one full tick)
                    order_price = best_ask - self.config.tick_size
                side = 'Bid'
            else:
                # For sell orders, choose price based on maker_aggressive flag
                if getattr(self.config, 'maker_aggressive', True):
                    # slightly above best bid (half-tick toward market)
                    order_price = best_bid + (self.config.tick_size / Decimal(2))
                else:
                    # original, more passive behavior (one full tick)
                    order_price = best_bid + self.config.tick_size
                side = 'Ask'

            # Place the order using Backpack SDK (post-only to ensure maker order)
            order_result = self.account_client.execute_order(
                symbol=contract_id,
                side=side,
                order_type=OrderTypeEnum.LIMIT,
                quantity=str(quantity),
                price=str(self.round_to_tick(order_price)),
                post_only=True,
                time_in_force=TimeInForceEnum.GTC
            )

            if not order_result:
                return OrderResult(success=False, error_message='Failed to place order')

            if 'code' in order_result:
                message = order_result.get('message', 'Unknown error')
                self.logger.log(f"[OPEN] Error placing order: {message}", "ERROR")

                # If insufficient margin, attempt to place a close order to free margin
                if 'Insufficient margin to open a new order' in message:
                    try:
                        self.logger.log("Insufficient margin detected; attempting to place close order to free margin", "INFO")
                        close_side = getattr(self.config, 'close_order_side', None)
                        if not close_side:
                            return OrderResult(success=False, error_message='Insufficient margin and no close_order_side configured')

                        # Choose an aggressive close price based on side to increase chance of execution
                        if close_side.lower() == 'sell':
                            close_price = self.round_to_tick(best_bid + self.config.tick_size)
                        else:
                            close_price = self.round_to_tick(best_ask - self.config.tick_size)

                        close_res = await self.place_close_order(contract_id, self.config.quantity, close_price, close_side)
                        return close_res
                    except Exception as e:
                        self.logger.log(f"Failed to place close order after insufficient margin error: {e}", "ERROR")
                        return OrderResult(success=False, error_message=str(e))

                continue

            # Extract order ID from response
            order_id = order_result.get('id')
            if not order_id:
                self.logger.log(f"[OPEN] No order ID in response: {order_result}", "ERROR")
                return OrderResult(success=False, error_message='No order ID in response')

            # Order successfully placed
            # Wait up to timeout for any fills; if no fills after timeout, cancel and place a market order
            timeout_seconds = getattr(self.config, 'order_timeout_seconds', 30)

            try:
                # Poll order info for up to timeout_seconds
                start = time.time()
                filled = Decimal(0)
                while time.time() - start < timeout_seconds:
                    await asyncio.sleep(1)
                    info = await self.get_order_info(order_id)
                    if info is None:
                        # Order not found -> treat as no-fill and continue waiting
                        continue
                    filled = Decimal(info.filled_size) if info.filled_size is not None else Decimal(0)
                    if filled >= Decimal(quantity):
                        # Fully filled within timeout
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.lower(),
                            size=quantity,
                            price=order_price,
                            status='FILLED'
                        )

                # Timeout expired. Only act if there was no fill at all.
                if filled == 0:
                    self.logger.log(f"Order {order_id} not filled after {timeout_seconds}s, cancelling and placing market order", "INFO")
                    # Cancel the original order
                    try:
                        await self.cancel_order(order_id)
                    except asyncio.CancelledError:
                        self.logger.log(f"Order wait cancelled while cancelling order {order_id}", "WARNING")
                        return OrderResult(success=False, error_message='Cancelled')

                    # Place market order to take liquidity
                    market_result = self.account_client.execute_order(
                        symbol=contract_id,
                        side=side,
                        order_type=OrderTypeEnum.MARKET,
                        quantity=str(quantity),
                        post_only=False
                    )

                    if not market_result or 'code' in market_result:
                        return OrderResult(success=False, error_message='Failed to place market replacement order')

                    new_order_id = market_result.get('id')
                    return OrderResult(success=True, order_id=new_order_id, side=side.lower(), size=quantity, price=Decimal(0), status='FILLED')
                else:
                    # Partial fill but not full; return current status
                    return OrderResult(success=True, order_id=order_id, side=side.lower(), size=quantity, price=order_price, status='PARTIALLY_FILLED')

            except asyncio.CancelledError:
                self.logger.log(f"place_open_order cancelled for order {order_id}", "WARNING")
                return OrderResult(success=False, error_message='Cancelled')
            except Exception as e:
                self.logger.log(f"Error while waiting for order {order_id}: {e}", "ERROR")
                return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded')

    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str) -> OrderResult:
        """Place a close order with Backpack using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 15
        retry_count = 0

        while retry_count < max_retries:
            retry_count += 1
            # Get current market prices to adjust order price if needed
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message='No bid/ask data available')

            # Adjust order price based on market conditions and side
            adjusted_price = price
            if side.lower() == 'sell':
                order_side = 'Ask'
                # For sell orders, ensure price is above best bid to be a maker order
                if price <= best_bid:
                    if getattr(self.config, 'maker_aggressive', True):
                        # slightly more aggressive half-tick toward market
                        adjusted_price = best_bid + (self.config.tick_size / Decimal(2))
                    else:
                        # original behavior: full tick
                        adjusted_price = best_bid + self.config.tick_size
            elif side.lower() == 'buy':
                order_side = 'Bid'
                # For buy orders, ensure price is below best ask to be a maker order
                if price >= best_ask:
                    if getattr(self.config, 'maker_aggressive', True):
                        adjusted_price = best_ask - (self.config.tick_size / Decimal(2))
                    else:
                        adjusted_price = best_ask - self.config.tick_size

            adjusted_price = self.round_to_tick(adjusted_price)
            # Place the order using Backpack SDK (post-only to avoid taker fees)
            order_result = self.account_client.execute_order(
                symbol=contract_id,
                side=order_side,
                order_type=OrderTypeEnum.LIMIT,
                quantity=str(quantity),
                price=str(adjusted_price),
                post_only=True,
                time_in_force=TimeInForceEnum.GTC
            )

            if not order_result:
                return OrderResult(success=False, error_message='Failed to place order')

            if 'code' in order_result:
                message = order_result.get('message', 'Unknown error')
                self.logger.log(f"[CLOSE] Error placing order: {message}", "ERROR")
                continue

            # Extract order ID from response
            order_id = order_result.get('id')
            if not order_id:
                self.logger.log(f"[CLOSE] No order ID in response: {order_result}", "ERROR")
                return OrderResult(success=False, error_message='No order ID in response')

            # Order successfully placed
            # Order successfully placed
            # Wait up to timeout for any fills; if no fills after timeout, cancel and place a market order
            timeout_seconds = getattr(self.config, 'order_timeout_seconds', 30)

            try:
                start = time.time()
                filled = Decimal(0)
                while time.time() - start < timeout_seconds:
                    await asyncio.sleep(1)
                    info = await self.get_order_info(order_id)
                    if info is None:
                        continue
                    filled = Decimal(info.filled_size) if info.filled_size is not None else Decimal(0)
                    if filled >= Decimal(quantity):
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.lower(),
                            size=quantity,
                            price=adjusted_price,
                            status='FILLED'
                        )

                # Timeout expired
                if filled == 0:
                    self.logger.log(f"Close order {order_id} not filled after {timeout_seconds}s, cancelling and placing market order", "INFO")
                    try:
                        await self.cancel_order(order_id)
                    except asyncio.CancelledError:
                        self.logger.log(f"place_close_order cancelled while cancelling order {order_id}", "WARNING")
                        return OrderResult(success=False, error_message='Cancelled')

                    # Determine API side for market order
                    api_side = order_side
                    market_result = self.account_client.execute_order(
                        symbol=contract_id,
                        side=api_side,
                        order_type=OrderTypeEnum.MARKET,
                        quantity=str(quantity),
                        post_only=False
                    )

                    if not market_result or 'code' in market_result:
                        return OrderResult(success=False, error_message='Failed to place market replacement close order')

                    new_order_id = market_result.get('id')
                    return OrderResult(success=True, order_id=new_order_id, side=side.lower(), size=quantity, price=Decimal(0), status='FILLED')
                else:
                    return OrderResult(success=True, order_id=order_id, side=side.lower(), size=quantity, price=adjusted_price, status='PARTIALLY_FILLED')

            except asyncio.CancelledError:
                self.logger.log(f"place_close_order cancelled for order {order_id}", "WARNING")
                return OrderResult(success=False, error_message='Cancelled')
            except Exception as e:
                self.logger.log(f"Error while waiting for close order {order_id}: {e}", "ERROR")
                return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded for close order')

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order with Backpack using official SDK."""
        try:
            # Cancel the order using Backpack SDK
            cancel_result = self.account_client.cancel_order(
                symbol=self.config.contract_id,
                order_id=order_id
            )

            if not cancel_result:
                return OrderResult(success=False, error_message='Failed to cancel order')
            if 'code' in cancel_result:
                message = cancel_result.get('message', 'Unknown error')
                self.logger.log(
                    f"[CLOSE] Failed to cancel order {order_id}: {message}", "WARNING")

                # Handle 'Order not found' more intelligently: it can mean the order was filled
                # or removed by the exchange. Try to infer executed quantity before giving up.
                if 'Order not found' in message or 'order not found' in message.lower():
                    try:
                        # Try to get order info (may return None)
                        info = await self.get_order_info(order_id)
                        if info is not None:
                            filled_size = Decimal(info.filled_size)
                            self.logger.log(f"Inferred filled_size from get_order_info: {filled_size}", "INFO")
                            return OrderResult(success=True, filled_size=filled_size)

                        # Fallback: query current account positions and assume at most the order quantity was filled
                        pos = await self.get_account_positions()
                        # Use min(order quantity, current position) as heuristic filled size
                        heuristic_filled = min(Decimal(self.config.quantity), Decimal(pos))
                        self.logger.log(f"Order not found; using heuristic filled_size={heuristic_filled} based on current position {pos}", "INFO")
                        return OrderResult(success=True, filled_size=heuristic_filled)

                    except Exception as e:
                        self.logger.log(f"Error while inferring filled size after 'Order not found': {e}", "ERROR")
                        return OrderResult(success=False, error_message=str(e))
                else:
                    # Other error codes: try to extract executedQuantity if present, otherwise assume no fill
                    executed = cancel_result.get('executedQuantity')
                    if executed is not None:
                        filled_size = Decimal(executed)
                    else:
                        filled_size = Decimal(0)
                    return OrderResult(success=True, filled_size=filled_size)
            else:
                filled_size = Decimal(cancel_result.get('executedQuantity', 0))
            return OrderResult(success=True, filled_size=filled_size)

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    @query_retry()
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information from Backpack using official SDK."""
        # Get order information using Backpack SDK
        order_result = self.account_client.get_open_order(
            symbol=self.config.contract_id,
            order_id=order_id
        )

        if not order_result:
            return None

        # Return the order data as OrderInfo
        return OrderInfo(
            order_id=order_result.get('id', ''),
            side=order_result.get('side', '').lower(),
            size=Decimal(order_result.get('quantity', 0)),
            price=Decimal(order_result.get('price', 0)),
            status=order_result.get('status', ''),
            filled_size=Decimal(order_result.get('executedQuantity', 0)),
            remaining_size=Decimal(order_result.get('quantity', 0)) - Decimal(order_result.get('executedQuantity', 0))
        )

    @query_retry(default_return=[])
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract using official SDK."""
        # Get active orders using Backpack SDK
        active_orders = self.account_client.get_open_orders(symbol=contract_id)

        if not active_orders:
            return []

        # Return the orders list as OrderInfo objects
        order_list = active_orders if isinstance(active_orders, list) else active_orders.get('orders', [])
        orders = []

        for order in order_list:
            if isinstance(order, dict):
                if order.get('side', '') == 'Bid':
                    side = 'buy'
                elif order.get('side', '') == 'Ask':
                    side = 'sell'
                orders.append(OrderInfo(
                    order_id=order.get('id', ''),
                    side=side,
                    size=Decimal(order.get('quantity', 0)),
                    price=Decimal(order.get('price', 0)),
                    status=order.get('status', ''),
                    filled_size=Decimal(order.get('executedQuantity', 0)),
                    remaining_size=Decimal(order.get('quantity', 0)) - Decimal(order.get('executedQuantity', 0))
                ))

        return orders

    @query_retry(default_return=0)
    async def get_account_positions(self) -> Decimal:
        """Get account positions using official SDK."""
        positions_data = self.account_client.get_open_positions()
        position_amt = 0
        for position in positions_data:
            if position.get('symbol', '') == self.config.contract_id:
                position_amt = abs(Decimal(position.get('netQuantity', 0)))
                break
        return position_amt

    async def place_market_order(self, contract_id: str, quantity: Decimal, side: str) -> OrderResult:
        """Place a market order to immediately execute."""
        try:
            # Convert side to API format
            api_side = 'Bid' if side.lower() == 'buy' else 'Ask'
            
            # Place market order
            order_result = self.account_client.execute_order(
                symbol=contract_id,
                side=api_side,
                order_type=OrderTypeEnum.MARKET,
                quantity=str(quantity),
                post_only=False
            )

            if not order_result or 'code' in order_result:
                error_msg = order_result.get('message', 'Unknown error') if order_result else 'Failed to place market order'
                return OrderResult(success=False, error_message=error_msg)

            order_id = order_result.get('id')
            if not order_id:
                return OrderResult(success=False, error_message='No order ID in response')

            return OrderResult(
                success=True,
                order_id=order_id,
                side=side.lower(),
                size=quantity,
                price=Decimal(0),  # Market order, price not known in advance
                status='FILLED'    # Assume market orders fill immediately
            )

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract ID for a ticker."""
        ticker = self.config.ticker
        if len(ticker) == 0:
            self.logger.log("Ticker is empty", "ERROR")
            raise ValueError("Ticker is empty")

        markets = self.public_client.get_markets()
        for market in markets:
            if (market.get('marketType', '') == 'PERP' and market.get('baseSymbol', '') == ticker and
                    market.get('quoteSymbol', '') == 'USDC'):
                self.config.contract_id = market.get('symbol', '')
                min_quantity = Decimal(market.get('filters', {}).get('quantity', {}).get('minQuantity', 0))
                self.config.tick_size = Decimal(market.get('filters', {}).get('price', {}).get('tickSize', 0))
                break

        if self.config.contract_id == '':
            self.logger.log("Failed to get contract ID for ticker", "ERROR")
            raise ValueError("Failed to get contract ID for ticker")

        if self.config.quantity < min_quantity:
            self.logger.log(f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}", "ERROR")
            raise ValueError(f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}")

        if self.config.tick_size == 0:
            self.logger.log("Failed to get tick size for ticker", "ERROR")
            raise ValueError("Failed to get tick size for ticker")

        return self.config.contract_id, self.config.tick_size
