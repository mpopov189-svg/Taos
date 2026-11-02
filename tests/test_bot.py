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


if __name__ == '__main__':
    unittest.main()

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
