"""
Unit tests for Binance Futures Open Interest collector.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from analysis.crypto_state import CryptoState
from analysis.crypto_state_store import CryptoStateStore
from collectors.binance_futures_oi import BinanceFuturesOICollector


@pytest.mark.asyncio
async def test_fetch_symbol_oi_ignores_gold():
    collector = BinanceFuturesOICollector()
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    res = await collector.fetch_symbol_oi(mock_client, "XAUUSDT")
    assert res is None
    mock_client.get.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_symbol_oi_success():
    collector = BinanceFuturesOICollector()
    mock_client = AsyncMock(spec=httpx.AsyncClient)

    # 12 periods of 5m data
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"sumOpenInterest": "1000.0", "timestamp": 1670000000000},
        {"sumOpenInterest": "1050.0", "timestamp": 1670000300000},
        {"sumOpenInterest": "1100.0", "timestamp": 1670003300000},
    ]
    mock_client.get.return_value = mock_response

    res = await collector.fetch_symbol_oi(mock_client, "BTCUSDT")
    assert res is not None
    curr_oi, change_1h = res
    assert curr_oi == 1100.0
    # (1100 - 1000) / 1000 * 100 = +10.0%
    assert change_1h == 10.0


@pytest.mark.asyncio
async def test_fetch_all_updates_store():
    store = CryptoStateStore()
    btc = CryptoState(symbol="btcusdt", base_asset="BTC")
    eth = CryptoState(symbol="ethusdt", base_asset="ETH")
    store._states = {"btcusdt": btc, "ethusdt": eth}

    collector = BinanceFuturesOICollector(store=store)

    async def fake_fetch_symbol_oi(client, symbol):
        if symbol == "btcusdt":
            return (50000.0, 5.2)
        elif symbol == "ethusdt":
            return (25000.0, -2.1)
        return None

    with patch.object(collector, "fetch_symbol_oi", side_effect=fake_fetch_symbol_oi):
        updated = await collector.fetch_all()
        assert updated == 2
        assert btc.open_interest == 50000.0
        assert btc.oi_change_1h_pct == 5.2
        assert eth.open_interest == 25000.0
        assert eth.oi_change_1h_pct == -2.1
