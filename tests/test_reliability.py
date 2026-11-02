import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import Store, Trader, FEE_ESTIMATE, loop


class Feed:
    mode = 'paper'

    def __init__(self, price=114.6):
        self.price = price

    def market(self):
        return [100 + 0.2 * i + 2 * math.sin(i * 1.4) for i in range(80)], self.price


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.env = patch.dict(os.environ, {'MODE': 'paper', 'REQUIRE_PERSISTENT_DATA': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_buy_restart_sell_preserves_account_and_full_ledger(self):
        store = Store(self.folder.name)
        Trader(store, Feed()).tick()
        bought = store.snapshot()
        self.assertEqual(bought['trades'], 1)
        self.assertEqual(len(bought['ledger']), 1)
        self.assertEqual(bought['position_cost_eur'], 50)
        self.assertAlmostEqual(bought['total_value'], round(500 - 50 * FEE_ESTIMATE, 2))
        restarted = Store(self.folder.name)
        self.assertEqual(restarted.state['ledger'], bought['ledger'])
        self.assertEqual(restarted.state['checks'], 1)
        self.assertEqual(restarted.state['starts'], 2)
        Trader(restarted, Feed(110)).tick()
        s = restarted.snapshot()
        self.assertEqual([t['side'] for t in s['ledger']], ['buy', 'sell'])
        self.assertEqual(s['trades'], 2)
        self.assertEqual(s['position_qty'], 0)
        self.assertAlmostEqual(s['paper_cash'] - 500, s['realized_eur'], places=9)
        self.assertAlmostEqual(s['ledger'][-1]['realized_eur'], s['realized_eur'])
        self.assertEqual(s['position_cost_eur'], 0)
        self.assertEqual(s['checks'], 2)

    def test_network_error_does_not_trade_and_recovery_is_recorded(self):
        store = Store(self.folder.name)
        feed = Feed(150)
        trader = Trader(store, feed)
        with patch.object(feed, 'market', side_effect=TimeoutError('market unavailable')):
            trader.tick()
        self.assertEqual(store.state['trades'], 0)
        self.assertEqual(store.state['checks'], 1)
        self.assertIn('market unavailable', store.state['last_error'])
        trader.tick()
        self.assertEqual(store.state['last_error'], '')
        self.assertTrue(store.state['last_success'])
        self.assertEqual(store.state['checks'], 2)
        self.assertEqual(len(store.state['decisions']), 2)
        self.assertTrue(store.state['decisions'][1]['error'])

    def test_corrupt_state_is_not_silently_replaced_with_500_eur(self):
        path = Path(self.folder.name) / 'state.json'
        path.write_text('{corrupt', encoding='utf-8')
        with self.assertRaises(json.JSONDecodeError):
            Store(self.folder.name)
        self.assertEqual(path.read_text(), '{corrupt')

    def test_failed_atomic_replace_preserves_last_saved_account(self):
        store = Store(self.folder.name)
        original = store.path.read_bytes()
        store.state['paper_cash'] = 123
        with patch('bot.os.replace', side_effect=OSError('disk write failed')):
            with self.assertRaises(OSError):
                store.save()
        self.assertEqual(store.path.read_bytes(), original)

    def test_required_disk_cannot_fall_back_to_ephemeral_storage(self):
        with patch.dict(os.environ, {'REQUIRE_PERSISTENT_DATA': '1'}), patch('bot.os.path.ismount', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'persistent disk'):
                Store(self.folder.name)

    def test_pause_during_network_wait_prevents_buy_without_blocking_dashboard(self):
        store = Store(self.folder.name)
        feed = Feed()
        entered, release = threading.Event(), threading.Event()
        original_market = feed.market
        def slow_market():
            entered.set()
            release.wait(3)
            return original_market()
        feed.market = slow_market
        thread = threading.Thread(target=Trader(store, feed).tick)
        thread.start()
        self.assertTrue(entered.wait(1))
        try:
            acquired = store.lock.acquire(timeout=0.5)
            self.assertTrue(acquired, 'Network request blocked dashboard state access')
            if acquired:
                store.state['paused'] = True
                store.lock.release()
        finally:
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(store.state['trades'], 0)
        self.assertEqual(store.state['status'], 'Пауза')

    def test_snapshot_cannot_mutate_account(self):
        store = Store(self.folder.name)
        snapshot = store.snapshot()
        snapshot['ledger'].append({'side': 'fake'})
        self.assertEqual(store.state['ledger'], [])

    def test_stopped_engine_does_not_execute_a_tick(self):
        stop = threading.Event()
        stop.set()
        store = Store(self.folder.name)
        loop(Trader(store, Feed()), stop)
        self.assertEqual(store.state['checks'], 0)

    def test_legacy_paper_position_recovers_its_fee_inclusive_cost(self):
        store = Store(self.folder.name)
        Trader(store, Feed()).tick()
        old = store.snapshot()
        old.pop('position_cost_eur')
        store.path.write_text(json.dumps(old), encoding='utf-8')
        restored = Store(self.folder.name)
        self.assertAlmostEqual(restored.state['position_cost_eur'], 50)


if __name__ == '__main__':
    unittest.main()
