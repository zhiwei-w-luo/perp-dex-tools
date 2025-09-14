#!/usr/bin/env python3
"""
Modular Trading Bot - Supports multiple exchanges
"""

import argparse
import asyncio
from trading_bot import TradingBot, TradingConfig
from exchanges import ExchangeFactory


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='Modular Trading Bot - Supports multiple exchanges')

    # Exchange selection
    parser.add_argument('--exchange', type=str, default='edgex',
                        choices=ExchangeFactory.get_supported_exchanges(),
                        help='Exchange to use (default: edgex). '
                             f'Available: {", ".join(ExchangeFactory.get_supported_exchanges())}')

    # Trading parameters
    parser.add_argument('--contract-id', type=str, default='10000002',
                        help='Contract ID (default: 10000002 for ETH-USDT on EdgeX, ETH_USDC_PERP for Backpack)')
    parser.add_argument('--quantity', type=float, default=0.1,
                        help='Order quantity (default: 0.1)')
    parser.add_argument('--take-profit', type=float, default=0.9,
                        help='Take profit in USDT (default: 0.9)')
    parser.add_argument('--direction', type=str, default='buy',
                        help='Direction of the bot (default: buy)')
    parser.add_argument('--max-orders', type=int, default=40,
                        help='Maximum number of active orders (default: 40)')
    parser.add_argument('--wait-time', type=int, default=450,
                        help='Wait time between orders in seconds (default: 450)')

    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_arguments()

    # Create configuration
    config = TradingConfig(
        contract_id=args.contract_id,
        quantity=args.quantity,
        take_profit=args.take_profit,
        direction=args.direction,
        max_orders=args.max_orders,
        wait_time=args.wait_time,
        exchange=args.exchange
    )

    # Create and run the bot
    bot = TradingBot(config)
    try:
        await bot.run()
    except Exception as e:
        print(f"Bot execution failed: {e}")
        # The bot's run method already handles graceful shutdown
        raise


if __name__ == "__main__":
    asyncio.run(main())
