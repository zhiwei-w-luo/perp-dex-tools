#!/usr/bin/env python3
"""
Modular Trading Bot - Supports multiple exchanges
"""

import argparse
import asyncio
from pathlib import Path
import sys
import dotenv
from decimal import Decimal
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
    parser.add_argument('--ticker', type=str, default='ETH',
                        help='Ticker (default: ETH)')
    parser.add_argument('--quantity', type=Decimal, default=Decimal(0.1),
                        help='Order quantity (default: 0.1)')
    parser.add_argument('--take-profit', type=Decimal, default=Decimal(0.02),
                        help='Take profit in USDT (default: 0.02)')
    parser.add_argument('--direction', type=str, default='buy', choices=['buy', 'sell'],
                        help='Direction of the bot (default: buy)')
    parser.add_argument('--max-orders', type=int, default=40,
                        help='Maximum number of active orders (default: 40)')
    parser.add_argument('--wait-time', type=int, default=450,
                        help='Wait time between orders in seconds (default: 450)')
    parser.add_argument('--env-file', type=str, default=".env",
                        help=".env file path (default: .env)")
    parser.add_argument('--grid-step', type=str, default='-100',
                        help='The minimum distance in percentage to the next close order price (default: -100)')
    parser.add_argument('--stop-price', type=Decimal, default=-1,
                        help='Price to stop trading and exit. Buy: exits if price >= stop-price.'
                        'Sell: exits if price <= stop-price. (default: -1, no stop)')
    parser.add_argument('--pause-price', type=Decimal, default=-1,
                        help='Pause trading and wait. Buy: pause if price >= pause-price.'
                        'Sell: pause if price <= pause-price. (default: -1, no pause)')
    parser.add_argument('--aster-boost', action='store_true',
                        help='Use the Boost mode for volume boosting')

    # Stop-loss / take-profit thresholds (percent). Example: 0.08 means 0.08%
    parser.add_argument('--stop-loss-threshold', type=Decimal, default=Decimal('0.08'),
                        help='Stop-loss threshold in percent (default: 0.08)')
    parser.add_argument('--take-profit-threshold', type=Decimal, default=Decimal('0.12'),
                        help='Take-profit threshold in percent (default: 0.12)')

    # Maker aggressiveness flags (default: aggressive half-tick)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--maker-aggressive', dest='maker_aggressive', action='store_true',
                       help='Use half-tick aggressive maker pricing (default)')
    group.add_argument('--no-maker-aggressive', dest='maker_aggressive', action='store_false',
                       help='Do not use maker aggressiveness; use original tick offsets')
    parser.set_defaults(maker_aggressive=True)

    # Global wide-range SL/TP (percent). Example: 5 means 5% move triggers global SL
    parser.add_argument('--global-stop-loss', type=Decimal, default=Decimal('5.0'),
                        help='Global stop-loss percent (default: 5.0)')
    parser.add_argument('--global-take-profit', type=Decimal, default=Decimal('10.0'),
                        help='Global take-profit percent (default: 10.0)')

    return parser.parse_args()


async def main():
    """Main entry point."""
    args = parse_arguments()

    # Validate aster-boost can only be used with aster exchange
    if args.aster_boost and args.exchange != 'aster':
        print(f"Error: --aster-boost can only be used when --exchange is 'aster'. "
              f"Current exchange: {args.exchange}")
        sys.exit(1)

    env_path = Path(args.env_file)
    if not env_path.exists():
        print(f"Env file not find: {env_path.resolve()}")
        sys.exit(1)
    dotenv.load_dotenv(args.env_file)

    # Create configuration
    config = TradingConfig(
        ticker=args.ticker,
        contract_id='',  # will be set in the bot's run method
        tick_size=Decimal(0),
        quantity=args.quantity,
        take_profit=args.take_profit,
        direction=args.direction,
        max_orders=args.max_orders,
        wait_time=args.wait_time,
        exchange=args.exchange,
        grid_step=Decimal(args.grid_step),
        stop_price=Decimal(args.stop_price),
        pause_price=Decimal(args.pause_price),
        aster_boost=args.aster_boost
        ,
        stop_loss_threshold=args.stop_loss_threshold,
        take_profit_threshold=args.take_profit_threshold
        ,
        maker_aggressive=args.maker_aggressive
        ,
        global_stop_loss_percent=args.global_stop_loss,
        global_take_profit_percent=args.global_take_profit
    )

    # Create and run the bot
    bot = TradingBot(config)
    try:
        await bot.run()
    except Exception as e:
        print(f"Bot execution failed: {e}")
        # The bot's run method already handles graceful shutdown
        return


if __name__ == "__main__":
    asyncio.run(main())
