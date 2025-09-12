"""
EdgeX exchange client implementation.
"""

import os
import asyncio
import json
import traceback
from typing import Dict, Any, List, Optional
from edgex_sdk import Client, OrderSide, WebSocketManager, CancelOrderParams, GetOrderBookDepthParams, GetActiveOrderParams

from .base import BaseExchangeClient, OrderResult, OrderInfo


class EdgeXClient(BaseExchangeClient):
    """EdgeX exchange client implementation."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize EdgeX client."""
        super().__init__(config)

        # EdgeX credentials from environment
        self.account_id = os.getenv('EDGEX_ACCOUNT_ID')
        self.stark_private_key = os.getenv('EDGEX_STARK_PRIVATE_KEY')
        self.base_url = os.getenv('EDGEX_BASE_URL', 'https://pro.edgex.exchange')
        self.ws_url = os.getenv('EDGEX_WS_URL', 'wss://quote.edgex.exchange')

        if not self.account_id or not self.stark_private_key:
            raise ValueError("EDGEX_ACCOUNT_ID and EDGEX_STARK_PRIVATE_KEY must be set in environment variables")

        # Initialize EdgeX client using official SDK
        self.client = Client(
            base_url=self.base_url,
            account_id=int(self.account_id),
            stark_private_key=self.stark_private_key
        )

        # Initialize WebSocket manager using official SDK
        self.ws_manager = WebSocketManager(
            base_url=self.ws_url,
            account_id=int(self.account_id),
            stark_pri_key=self.stark_private_key
        )

        self._order_update_handler = None

    def _validate_config(self) -> None:
        """Validate EdgeX configuration."""
        required_env_vars = ['EDGEX_ACCOUNT_ID', 'EDGEX_STARK_PRIVATE_KEY']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    async def connect(self) -> None:
        """Connect to EdgeX WebSocket."""
        self.ws_manager.connect_private()

    async def disconnect(self) -> None:
        """Disconnect from EdgeX."""
        try:
            if hasattr(self, 'client') and self.client:
                await self.client.close()
            if hasattr(self, 'ws_manager'):
                self.ws_manager.disconnect_all()
        except Exception as e:
            print(f"Error during EdgeX disconnect: {e}")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "edgex"

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

        def order_update_handler(message):
            """Handle order updates from WebSocket."""
            try:
                # Parse the message structure
                if isinstance(message, str):
                    message = json.loads(message)

                # Check if this is a trade-event with ORDER_UPDATE
                if (message.get('type') == 'trade-event' and
                        message.get('content', {}).get('event') == 'ORDER_UPDATE'):

                    # Extract order data from the nested structure
                    content = message.get('content', {})
                    data = content.get('data', {})
                    orders = data.get('order', [])

                    if orders and len(orders) > 0:
                        order = orders[0]  # Get the first order
                        order_id = order.get('id')
                        status = order.get('status')
                        side = order.get('side', '').lower()

                        if self._order_update_handler:
                            self._order_update_handler({
                                'order_id': order_id,
                                'side': side,
                                'status': status,
                                'size': order.get('size'),
                                'price': order.get('price'),
                                'contract_id': order.get('contractId')
                            })

            except Exception as e:
                print(f"Error handling order update: {e}")
                print(f"Traceback: {traceback.format_exc()}")

        # Subscribe to order updates
        self.ws_manager.subscribe_order_update(order_update_handler)

    async def place_open_order(self, contract_id: str, quantity: float, direction: str) -> OrderResult:
        """Place an open order with EdgeX using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 15
        retry_count = 0

        while retry_count < max_retries:
            try:
                depth_params = GetOrderBookDepthParams(contract_id=contract_id, limit=15)
                order_book = await self.client.quote.get_order_book_depth(depth_params)

                # Handle the response format: {"code": "SUCCESS", "data": [{"asks": [...], "bids": [...]}]}
                if not isinstance(order_book, dict) or 'data' not in order_book:
                    return OrderResult(success=False, error_message='Unexpected order book response format')

                order_book_data = order_book['data']
                if not isinstance(order_book_data, list) or len(order_book_data) == 0:
                    return OrderResult(success=False, error_message='Order book data is not a valid list')

                # Get the first (and should be only) order book entry
                order_book_entry = order_book_data[0]
                if not isinstance(order_book_entry, dict):
                    return OrderResult(success=False, error_message='Order book entry is not a dict')

                # Extract bids and asks from the entry
                bids = order_book_entry.get('bids', [])
                asks = order_book_entry.get('asks', [])

                if not bids or not asks:
                    return OrderResult(success=False, error_message='No bid/ask data available')

                # Best bid is the highest price someone is willing to buy at
                best_bid = float(bids[0]['price']) if bids and len(bids) > 0 else 0
                # Best ask is the lowest price someone is willing to sell at
                best_ask = float(asks[0]['price']) if asks and len(asks) > 0 else 0

                if best_bid <= 0 or best_ask <= 0:
                    return OrderResult(success=False, error_message='Invalid bid/ask prices')

                # Calculate order price based on direction
                if contract_id == '10000001':
                    price_delta = 0.1
                else:
                    price_delta = 0.01

                if direction == 'buy':
                    # For buy orders, place slightly below best ask to ensure execution
                    order_price = best_ask - price_delta
                    side = OrderSide.BUY
                else:
                    # For sell orders, place slightly above best bid to ensure execution
                    order_price = best_bid + price_delta
                    side = OrderSide.SELL

                # Place the order using official SDK (post-only to ensure maker order)
                order_result = await self.client.create_limit_order(
                    contract_id=contract_id,
                    size=str(quantity),
                    price=str(round(order_price, 2)),
                    side=side,
                    post_only=True
                )

                if not order_result or 'data' not in order_result:
                    return OrderResult(success=False, error_message='Failed to place order')

                # Extract order ID from response
                order_id = order_result['data'].get('orderId')
                if not order_id:
                    return OrderResult(success=False, error_message='No order ID in response')

                # Check order status after a short delay to see if it was rejected
                await asyncio.sleep(0.01)
                order_info = await self.get_order_info(order_id)

                if order_info:
                    if order_info.status == 'CANCELED':
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            continue
                        else:
                            return OrderResult(success=False, error_message=f'Order rejected after {max_retries} attempts')
                    elif order_info.status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED']:
                        # Order successfully placed
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.value,
                            size=quantity,
                            price=order_price,
                            status=order_info.status
                        )
                    else:
                        return OrderResult(success=False, error_message=f'Unexpected order status: {order_info.status}')
                else:
                    # Assume order is successful if we can't get info
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side.value,
                        size=quantity,
                        price=order_price
                    )

            except Exception as e:
                if retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1)  # Wait before retry
                    continue
                else:
                    return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded')

    async def place_close_order(self, contract_id: str, quantity: float, price: float, side: str) -> OrderResult:
        """Place a close order with EdgeX using official SDK with retry logic for POST_ONLY rejections."""
        max_retries = 15
        retry_count = 0

        while retry_count < max_retries:
            try:
                # Get current market prices to adjust order price if needed
                depth_params = GetOrderBookDepthParams(contract_id=contract_id, limit=15)
                order_book = await self.client.quote.get_order_book_depth(depth_params)

                if not isinstance(order_book, dict) or 'data' not in order_book:
                    return OrderResult(success=False, error_message='Failed to get order book')

                order_book_data = order_book['data']
                if not isinstance(order_book_data, list) or len(order_book_data) == 0:
                    return OrderResult(success=False, error_message='Invalid order book data')

                # Get the first order book entry
                order_book_entry = order_book_data[0]
                bids = order_book_entry.get('bids', [])
                asks = order_book_entry.get('asks', [])

                if not bids or not asks:
                    return OrderResult(success=False, error_message='No bid/ask data available')

                # Get best bid and ask prices
                best_bid = float(bids[0]['price']) if bids and len(bids) > 0 else 0
                best_ask = float(asks[0]['price']) if asks and len(asks) > 0 else 0

                if best_bid <= 0 or best_ask <= 0:
                    return OrderResult(success=False, error_message='Invalid bid/ask prices')

                # Convert side string to OrderSide enum
                order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL

                # Adjust order price based on market conditions and side
                adjusted_price = price
                if contract_id == '10000001':
                    price_delta = 0.1
                else:
                    price_delta = 0.01
                if side.lower() == 'sell':
                    # For sell orders, ensure price is above best bid to be a maker order
                    if price <= best_bid:
                        adjusted_price = best_bid + price_delta
                elif side.lower() == 'buy':
                    # For buy orders, ensure price is below best ask to be a maker order
                    if price >= best_ask:
                        adjusted_price = best_ask - price_delta

                # Place the order using official SDK (post-only to avoid taker fees)
                order_result = await self.client.create_limit_order(
                    contract_id=contract_id,
                    size=str(quantity),
                    price=str(round(adjusted_price, 2)),
                    side=order_side,
                    post_only=True
                )

                if not order_result or 'data' not in order_result:
                    return OrderResult(success=False, error_message='Failed to place order')

                # Extract order ID from response
                order_id = order_result['data'].get('orderId')
                if not order_id:
                    return OrderResult(success=False, error_message='No order ID in response')

                # Check order status after a short delay to see if it was rejected
                await asyncio.sleep(0.01)
                order_info = await self.get_order_info(order_id)

                if order_info:
                    if order_info.status == 'CANCELED':
                        if retry_count < max_retries - 1:
                            retry_count += 1
                            continue
                        else:
                            return OrderResult(success=False, error_message=f'Close order rejected after {max_retries} attempts')
                    elif order_info.status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED']:
                        # Order successfully placed
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side,
                            size=quantity,
                            price=adjusted_price,
                            status=order_info.status
                        )
                    else:
                        return OrderResult(success=False, error_message=f'Unexpected close order status: {order_info.status}')
                else:
                    # Assume order is successful if we can't get info
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side,
                        size=quantity,
                        price=adjusted_price
                    )

            except Exception as e:
                if retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1)  # Wait before retry
                    continue
                else:
                    return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded for close order')

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order with EdgeX using official SDK."""
        try:
            # Create cancel parameters using official SDK
            cancel_params = CancelOrderParams(order_id=order_id)

            # Cancel the order using official SDK
            cancel_result = await self.client.cancel_order(cancel_params)

            if not cancel_result or 'data' not in cancel_result:
                return OrderResult(success=False, error_message='Failed to cancel order')

            return OrderResult(success=True)

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information from EdgeX using official SDK."""
        try:
            # Use the newly created get_order_by_id method
            order_result = await self.client.order.get_order_by_id(order_id_list=[order_id])

            if not order_result or 'data' not in order_result:
                return None

            # The API returns a list of orders, get the first (and should be only) one
            order_list = order_result['data']
            if order_list and len(order_list) > 0:
                order_data = order_list[0]
                return OrderInfo(
                    order_id=order_data.get('id', ''),
                    side=order_data.get('side', '').lower(),
                    size=float(order_data.get('size', 0)),
                    price=float(order_data.get('price', 0)),
                    status=order_data.get('status', ''),
                    filled_size=float(order_data.get('cumFillSize', 0)),
                    remaining_size=float(order_data.get('size', 0)) - float(order_data.get('cumFillSize', 0))
                )

            return None

        except Exception:
            return None

    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract using official SDK."""
        try:
            # Get active orders using official SDK
            params = GetActiveOrderParams(size="100", offset_data="")
            active_orders = await self.client.get_active_orders(params)

            if not active_orders or 'data' not in active_orders:
                return []

            # Filter orders for the specific contract and ensure they are dictionaries
            # The API returns orders under 'dataList' key, not 'orderList'
            order_list = active_orders['data'].get('dataList', [])
            contract_orders = []

            for order in order_list:
                if isinstance(order, dict) and order.get('contractId') == contract_id:
                    contract_orders.append(OrderInfo(
                        order_id=order.get('id', ''),
                        side=order.get('side', '').lower(),
                        size=float(order.get('size', 0)),
                        price=float(order.get('price', 0)),
                        status=order.get('status', ''),
                        filled_size=float(order.get('cumFillSize', 0)),
                        remaining_size=float(order.get('size', 0)) - float(order.get('cumFillSize', 0))
                    ))

            return contract_orders

        except Exception:
            return []

    async def get_account_positions(self) -> Dict[str, Any]:
        """Get account positions using official SDK."""
        try:
            positions = await self.client.get_account_positions()
            return positions
        except Exception:
            return {}
