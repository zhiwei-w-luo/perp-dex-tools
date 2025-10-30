import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch
import importlib.util
from pathlib import Path as _Path

# Import BackpackClient directly from file to avoid importing package-level dependencies
bp_path = _Path(__file__).parent.parent / 'exchanges' / 'backpack.py'
spec = importlib.util.spec_from_file_location('backpack', str(bp_path))
backpack_mod = importlib.util.module_from_spec(spec)
import types

# Provide a minimal fake 'bpx' package to prevent import errors when loading backpack.py in tests
bpx_mod = types.ModuleType('bpx')
bpx_public = types.ModuleType('bpx.public')
class _Public:
    def __init__(self, *a, **k):
        pass
setattr(bpx_public, 'Public', _Public)

bpx_account = types.ModuleType('bpx.account')
class _Account:
    def __init__(self, *a, **k):
        pass
setattr(bpx_account, 'Account', _Account)

bpx_constants = types.ModuleType('bpx.constants')
bpx_constants.enums = types.ModuleType('bpx.constants.enums')
class _OrderTypeEnum:
    LIMIT = 'LIMIT'
    MARKET = 'MARKET'
class _TimeInForceEnum:
    GTC = 'GTC'
setattr(bpx_constants.enums, 'OrderTypeEnum', _OrderTypeEnum)
setattr(bpx_constants.enums, 'TimeInForceEnum', _TimeInForceEnum)

import sys as _sys
_sys.modules['bpx'] = bpx_mod
_sys.modules['bpx.public'] = bpx_public
_sys.modules['bpx.account'] = bpx_account
_sys.modules['bpx.constants'] = bpx_constants
_sys.modules['bpx.constants.enums'] = bpx_constants.enums

spec.loader.exec_module(backpack_mod)
BackpackClient = backpack_mod.BackpackClient

# Import OrderInfo/OrderResult from base module directly (base is lightweight)
base_path = _Path(__file__).parent.parent / 'exchanges' / 'base.py'
spec_base = importlib.util.spec_from_file_location('base', str(base_path))
base_mod = importlib.util.module_from_spec(spec_base)
spec_base.loader.exec_module(base_mod)
OrderInfo = base_mod.OrderInfo
OrderResult = base_mod.OrderResult


class DummyConfig:
    def __init__(self):
        self.contract_id = 'TEST-PAIR'
        self.tick_size = Decimal('0.01')
        self.quantity = Decimal('1')
        self.ticker = 'TEST'
        self.close_order_side = 'sell'
        # We will override order_timeout_seconds per-test


async def _test_cancel_and_replace():
    """If the limit order gets no fills within timeout, it should be canceled and a market order placed."""
    # Prepare client with very small timeout so logic takes the cancel path immediately
    cfg = DummyConfig()
    cfg.order_timeout_seconds = 0

    # Ensure env vars so constructor does not raise
    import os
    os.environ['BACKPACK_PUBLIC_KEY'] = 'pk'
    os.environ['BACKPACK_SECRET_KEY'] = 'sk'

    client = BackpackClient(cfg)

    # Mock account_client.execute_order: first call (limit) -> return id lim1; second call (market) -> id mkt1
    client.account_client = Mock()
    client.account_client.execute_order = Mock(side_effect=[{'id': 'lim1'}, {'id': 'mkt1'}])

    # Mock cancel_order to avoid hitting real API
    client.cancel_order = AsyncMock(return_value=OrderResult(success=True, filled_size=Decimal(0)))

    # Run place_open_order and verify market replacement used
    result = await client.place_open_order(cfg.contract_id, cfg.quantity, 'buy')

    print('test_cancel_and_replace result:', result)
    assert result.success is True
    assert result.order_id == 'mkt1'


async def _test_no_cancel_if_filled():
    """If the limit order is fully filled quickly, there should be no cancel or market order."""
    cfg = DummyConfig()
    cfg.order_timeout_seconds = 2

    import os
    os.environ['BACKPACK_PUBLIC_KEY'] = 'pk'
    os.environ['BACKPACK_SECRET_KEY'] = 'sk'

    client = BackpackClient(cfg)

    # Mock the initial limit order placement
    client.account_client = Mock()
    client.account_client.execute_order = Mock(return_value={'id': 'lim1'})

    # Make get_order_info return a fully filled order on first check
    filled_info = OrderInfo(order_id='lim1', side='buy', size=cfg.quantity, price=Decimal('1'), status='FILLED', filled_size=cfg.quantity)
    client.get_order_info = AsyncMock(return_value=filled_info)

    # Patch asyncio.sleep to return immediately to speed up the test
    with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
        result = await client.place_open_order(cfg.contract_id, cfg.quantity, 'buy')

    print('test_no_cancel_if_filled result:', result)
    assert result.success is True
    assert result.order_id == 'lim1'
    assert result.status == 'FILLED'


async def run_tests():
    print('\n=== Running Backpack order-timeout tests ===')
    await _test_cancel_and_replace()
    await _test_no_cancel_if_filled()
    print('All tests passed')


if __name__ == '__main__':
    asyncio.run(run_tests())
