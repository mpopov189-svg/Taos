import unittest
from bot import ema, rsi, evaluate, dformat, STOP_LOSS, TAKE_PROFIT


class BotTests(unittest.TestCase):
    def test_ema_constant(self):
        self.assertEqual(ema([200.0] * 55, 10), 200.0)

    def test_rsi_constant(self):
        self.assertEqual(rsi([100.0] * 40), 50.0)

    def test_downtrend_no_buy(self):
        prices = [1000.0 - i * 5 for i in range(70)]
        self.assertEqual(evaluate(prices, prices[-1], False, 0, 0)[0], 'hold')

    def test_stop_loss(self):
        prices = [100 + i for i in range(60)]
        self.assertEqual(evaluate(prices, 96.9, True, 100, 104)[0], 'sell')

    def test_take_profit(self):
        prices = [100 + i for i in range(60)]
        self.assertEqual(evaluate(prices, 106.0, True, 100, 106)[0], 'sell')

    def test_round_lot(self):
        self.assertEqual(dformat(0.003154, '0.00001'), '0.00315')


class FakeAPI:
    mode = 'paper'
    def __init__(self):
        self.price = 150.0
        import math
        self.closes = [100 + 0.2*i + 2*math.sin(i*1.4) for i in range(80)]
    def market(self):
        return self.closes, self.price


class TraderIntegrationTests(unittest.TestCase):
    def test_paper_buy_and_protective_sell_no_cooldown_block(self):
        import tempfile
        from bot import Store, Trader
        with tempfile.TemporaryDirectory() as d:
            api = FakeAPI()
            store = Store(d)
            trader = Trader(store, api)
            # buy signal near EMA10, with moderate RSI.
            api.price = 114.6
            trader.tick()
            self.assertEqual(store.state['trades'], 1)
            self.assertGreater(store.state['paper_btc'], 0)
            # Price suddenly drops; sell protection must bypass cooldown.
            api.price = 110
            trader.tick()
            self.assertEqual(store.state['trades'], 2)
            self.assertEqual(store.state['position_qty'], 0)
            self.assertLess(store.state['paper_cash'], 500)


class PublicMarketTests(unittest.TestCase):
    def fixture(self, now):
        from datetime import datetime, timezone
        hour = int(now // 3600) * 3600
        candles = [[hour - i * 3600, 90, 200, 100, 9999 if i == 0 else 100 + i, 1]
                   for i in range(41)]
        ticker = {'price': '125.00', 'time': datetime.fromtimestamp(now, timezone.utc).isoformat()}
        return candles, ticker

    def test_open_candle_excluded_and_hourly_history_cached(self):
        from unittest.mock import patch
        from bot import CoinbasePaperMarket
        now = 2000000000
        candles, ticker = self.fixture(now)
        market = CoinbasePaperMarket()
        with patch('bot.time.time', return_value=now), patch.object(
                market, '_get', side_effect=[candles, ticker, ticker]) as get:
            closes, price = market.market()
            self.assertEqual(len(closes), 40)
            self.assertNotIn(9999, closes)
            self.assertEqual(closes[-1], 101)
            self.assertEqual(price, 125)
            market.market()
            self.assertEqual(get.call_count, 3)

    def test_stale_ticker_is_rejected(self):
        from unittest.mock import patch
        from bot import CoinbasePaperMarket
        now = 2000000000
        candles, _ = self.fixture(now)
        _, ticker = self.fixture(now - 1000)
        market = CoinbasePaperMarket()
        with patch('bot.time.time', return_value=now), patch.object(
                market, '_get', side_effect=[candles, ticker]):
            with self.assertRaisesRegex(RuntimeError, 'остаряла'):
                market.market()


if __name__ == '__main__':
    unittest.main()
