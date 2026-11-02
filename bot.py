"""Misho AutoBot: one-pair automatic spot bot (paper by default).

This is RULE-BASED algorithmic trading, not predictive AI. Not investment advice.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import math
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from urllib.parse import urlencode

from urllib.request import Request, urlopen

BASE_URL = "https://eea.okx.com"  # Official EEA API gateway
SYMBOL = "BTC-EUR"
BASE_CCY, QUOTE_CCY = "BTC", "EUR"
POLL_SECONDS = 90
MAX_ORDER_EUR = 50.0
CAPITAL_FRACTION = 0.15
STOP_LOSS = 0.03
TAKE_PROFIT = 0.05
TRAILING_STOP = 0.025
COOLDOWN_SECONDS = 6 * 3600
FEE_ESTIMATE = 0.0015  # paper-only conservative approximation, not an OKX quote


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dformat(v: float, step: str):
    """Round down to instrument lotSz, preserving exponent precision."""
    increment = Decimal(str(step))
    n = (Decimal(str(v)) / increment).to_integral_value(rounding=ROUND_DOWN) * increment
    return format(n, 'f')


def ema(values, window):
    if len(values) < window:
        raise ValueError("Insufficient candles")
    alpha = 2.0 / (window + 1.0)
    out = sum(values[:window]) / window
    for v in values[window:]:
        out = alpha * v + (1.0 - alpha) * out
    return out


def rsi(values, period=14):
    if len(values) < period + 1:
        raise ValueError("Insufficient candles for RSI")
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(v, 0.0) for v in diffs]
    losses = [max(-v, 0.0) for v in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(diffs)):
        avg_gain = ((period - 1) * avg_gain + gains[i]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i]) / period
    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def evaluate(closes, price, position, entry, high):
    """Return action and explanation; does not place orders."""
    fast = ema(closes, 10)
    slow = ema(closes, 30)
    momentum = rsi(closes, 14)
    if position:
        if price <= entry * (1 - STOP_LOSS):
            return 'sell', 'Защита: спад под -3%', fast, slow, momentum
        if price >= entry * (1 + TAKE_PROFIT):
            return 'sell', 'Прибиране на печалба: +5%', fast, slow, momentum
        if high >= entry * 1.015 and price <= high * (1 - TRAILING_STOP):
            return 'sell', 'Трейлинг защита от спад', fast, slow, momentum
        if fast < slow:
            return 'sell', 'Низходящ тренд', fast, slow, momentum
        return 'hold', 'Задържане на позицията', fast, slow, momentum
    if fast > slow and 47 <= momentum <= 67 and price <= fast * 1.018:
        return 'buy', 'Възходящ тренд с умерен импулс', fast, slow, momentum
    return 'hold', 'Няма достатъчно добър сигнал', fast, slow, momentum


def default_state():
    return {
        'paused': False, 'paper_cash': 500.0, 'paper_btc': 0.0,
        'position_qty': 0.0, 'entry': 0.0, 'high': 0.0,
        'last_trade': 0.0, 'realized_eur': 0.0, 'trades': 0,
        'history': [], 'pending': None, 'halted': False,
        'last_error': '', 'status': 'Стартиране', 'last_check': '',
        'last_price': 0, 'last_signal': 'Очаква данни', 'total_value': 500.0,
        'mode': os.environ.get('MODE', 'paper').strip().lower(),
    }


class Store:
    def __init__(self, folder):
        self.path = Path(folder) / 'state.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if self.path.exists():
            with self.path.open('r', encoding='utf-8') as f:
                self.state = json.load(f)
        else:
            self.state = default_state()
            self.save()
        mode = os.environ.get('MODE', 'paper').strip().lower()
        if self.state['mode'] != mode:
            # Prevent accidentally taking paper positions live or switching state contexts.
            if self.state['trades'] or self.state['position_qty'] or self.state.get('pending'):
                raise RuntimeError('Existing state is for another mode. Use a new empty DATA_DIR.')
            self.state['mode'] = mode
            self.save()

    def save(self):
        with self.lock:
            payload = json.dumps(self.state, ensure_ascii=False, indent=2)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=self.path.parent, prefix='.state-', delete=False) as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
                filename = f.name
            os.replace(filename, self.path)

    def log(self, text, price=None):
        with self.lock:
            self.state['history'].insert(0, {'time': now_utc(), 'message': text, 'price': price})
            self.state['history'] = self.state['history'][:40]
            self.state['status'] = text
            self.save()


class OKX:
    def __init__(self, mode='paper'):
        self.mode = mode
        self.base_url = BASE_URL
        self.key = os.getenv('OKX_API_KEY', '')
        self.secret = os.getenv('OKX_API_SECRET', '')
        self.passphrase = os.getenv('OKX_API_PASSPHRASE', '')
        if mode in ('okx_demo', 'live') and not (self.key and self.secret and self.passphrase):
            raise RuntimeError('OKX API key, secret and passphrase are needed for this mode.')
        if mode == 'live':
            if os.getenv('ENABLE_LIVE') != '1' or os.getenv('LIVE_RISK_ACK') != 'I_ACCEPT_REAL_LOSSES':
                raise RuntimeError('LIVE mode requires ENABLE_LIVE=1 and LIVE_RISK_ACK=I_ACCEPT_REAL_LOSSES.')
            if os.getenv('PERSISTENT_VOLUME_CONFIRMED') != 'YES':
                raise RuntimeError('LIVE mode requires confirmed persistent DATA_DIR volume.')
            if not os.getenv('BOT_PASSWORD'):
                raise RuntimeError('Set BOT_PASSWORD for live trading dashboard.')

    def _request(self, method, path, params=None, data=None, private=False):
        if params:
            path += '?' + urlencode(params)
        body = json.dumps(data, separators=(',', ':')) if data is not None else ''
        headers = {'Content-Type': 'application/json'}
        if self.mode == 'okx_demo':
            headers['x-simulated-trading'] = '1'
        if private:
            stamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            signature = base64.b64encode(hmac.new(
                self.secret.encode(), (stamp + method.upper() + path + body).encode(), hashlib.sha256
            ).digest()).decode()
            headers.update({'OK-ACCESS-KEY': self.key, 'OK-ACCESS-PASSPHRASE': self.passphrase,
                            'OK-ACCESS-TIMESTAMP': stamp, 'OK-ACCESS-SIGN': signature})
        req = Request(self.base_url + path, data=body.encode() if data is not None else None,
                      headers=headers, method=method.upper())
        with urlopen(req, timeout=15) as response:
            result = json.loads(response.read().decode('utf-8'))
        if result.get('code') != '0':
            raise RuntimeError('OKX error: ' + str(result.get('code')) + ' ' + str(result.get('msg'))[:140])
        return result.get('data', [])

    def market(self):
        # Closed hourly candles only: current in-progress candle is excluded.
        candles = self._request('GET', '/api/v5/market/candles', {'instId': SYMBOL, 'bar': '1H', 'limit': '100'})
        completed = sorted((x for x in candles if x[8] == '1'), key=lambda x: int(x[0]))
        closes = [float(x[4]) for x in completed]
        if len(closes) < 35 or any(v <= 0 for v in closes):
            raise RuntimeError('Insufficient confirmed BTC-EUR candles')
        ticker = self._request('GET', '/api/v5/market/ticker', {'instId': SYMBOL})
        price = float(ticker[0]['last'])
        if price <= 0 or not math.isfinite(price):
            raise RuntimeError('Invalid market price')
        return closes, price

    def instrument(self):
        d = self._request('GET', '/api/v5/public/instruments', {'instType': 'SPOT', 'instId': SYMBOL})
        if not d or d[0].get('state') != 'live':
            raise RuntimeError('BTC-EUR is unavailable for SPOT trading in this region/account')
        return d[0]

    def balances(self):
        result = self._request('GET', '/api/v5/account/balance', {'ccy': 'EUR,BTC'}, private=True)
        entries = result[0].get('details', [])
        return {c: float(next((x.get('availBal') or '0' for x in entries if x['ccy'] == c), '0'))
                for c in ('EUR', 'BTC')}

    def submit(self, side, size, target, order_id):
        payload = {'instId': SYMBOL, 'tdMode': 'cash', 'clOrdId': order_id,
                   'side': side, 'ordType': 'market', 'tgtCcy': target,
                   'sz': str(size), 'banAmend': 'true'}
        answer = self._request('POST', '/api/v5/trade/order', data=payload, private=True)
        if not answer or answer[0].get('sCode') != '0':
            raise RuntimeError('Order rejected: ' + str(answer[0].get('sMsg') if answer else 'empty response'))
        return answer[0].get('ordId')

    def find_order(self, cl_id):
        d = self._request('GET', '/api/v5/trade/order', {'instId': SYMBOL, 'clOrdId': cl_id}, private=True)
        if not d:
            raise RuntimeError('Order cannot be found, manual reconciliation required')
        return d[0]


class Trader:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.action_lock = threading.Lock()

    def reconcile(self):
        s = self.store.state
        pending = s.get('pending')
        if not pending:
            return
        order = self.api.find_order(pending['clOrdId'])
        if order['state'] not in ('filled', 'canceled'):
            s['status'] = 'Чака потвърждение на борсова поръчка'
            self.store.save()
            return
        filled = float(order.get('accFillSz') or '0')
        avg = float(order.get('avgPx') or '0')
        if filled > 0 and avg <= 0:
            raise RuntimeError('Cannot determine filled order price — manual review required')
        if filled > 0:
            if pending['side'] == 'buy':
                s['position_qty'] += filled
                s['entry'] = avg
                s['high'] = avg
            else:
                sold = min(filled, s['position_qty'])
                s['position_qty'] -= sold
                s['realized_eur'] += (avg - s['entry']) * sold
                # Do not endlessly try to sell residual dust after rounding/fees.
                if s['position_qty'] * avg < 3.0:
                    s['position_qty'] = 0
                    s['entry'] = 0
                    s['high'] = 0
            s['last_trade'] = time.time()
            s['trades'] += 1
        s['pending'] = None
        self.store.log(('КУПУВА' if pending['side'] == 'buy' else 'ПРОДАВА') +
                       f' {filled:.8f} BTC по {avg:.2f} EUR', avg)

    def tick(self):
        if not self.action_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        except Exception as exc:
            self.store.state['last_error'] = str(exc)[:240]
            self.store.state['status'] = 'Грешка — нови сделки не са изпълнени'
            self.store.save()
        finally:
            self.action_lock.release()

    def _tick(self):
        s = self.store.state
        s['last_check'] = now_utc()
        if s.get('halted'):
            s['status'] = 'СПРЯН ЗАРАДИ НЕИЗЯСНЕНА ПОРЪЧКА'
            self.store.save()
            return
        if self.api.mode != 'paper' and s.get('pending'):
            try:
                self.reconcile()
            except Exception:
                s['halted'] = True  # Fail closed: never duplicate a possibly submitted order.
                self.store.save()
                raise
            if s.get('pending'):
                return
        closes, price = self.api.market()
        s['last_price'] = price
        s['high'] = max(s['high'], price) if s['position_qty'] else 0
        position = s['position_qty'] > 0
        action, reason, fast, slow, strength = evaluate(closes, price, position, s['entry'], s['high'])
        s['last_signal'] = f'{reason} · RSI {strength:.0f} · EMA10 {fast:.0f} / EMA30 {slow:.0f}'
        if self.api.mode == 'paper':
            s['total_value'] = round(s['paper_cash'] + s['paper_btc'] * price, 2)
        else:
            balances = self.api.balances()
            s['total_value'] = round(balances['EUR'] + balances['BTC'] * price, 2)
            # This bot must control a dedicated clean account and avoid touching unrelated BTC.
            if not position and balances['BTC'] * price > 3.0:
                s['status'] = 'ОТКАЗ: наличен BTC извън бота (използвайте отделна чиста сметка)'
                self.store.save()
                return
        if s['paused']:
            s['status'] = 'Пауза'
            self.store.save()
            return
        if action == 'buy' and s['last_trade'] and time.time() - s['last_trade'] < COOLDOWN_SECONDS:
            s['status'] = 'Изчаква предпазния интервал преди нова покупка'
            self.store.save()
            return
        if action == 'hold':
            s['status'] = reason
            s['last_error'] = ''
            self.store.save()
            return
        if action == 'buy':
            cash = s['paper_cash'] if self.api.mode == 'paper' else balances['EUR']
            spend = min(MAX_ORDER_EUR, cash * CAPITAL_FRACTION)
            # Keep an extra reserve so fees cannot exceed available funds.
            spend = math.floor(spend * 100) / 100
            if spend < 10 or cash < spend * 1.02:
                s['status'] = 'Недостатъчен свободен баланс за покупка'
                self.store.save()
                return
            if self.api.mode == 'paper':
                qty = spend * (1 - FEE_ESTIMATE) / price
                s['paper_cash'] -= spend
                s['paper_btc'] += qty
                s['position_qty'] = qty
                s['entry'] = price
                s['high'] = price
                self.store.log(f'ДЕМО КУПУВА {qty:.8f} BTC за {spend:.2f} EUR', price)
            else:
                self.api.instrument()  # Verify allowed market
                self._submit_safe('buy', str(spend), 'quote_ccy')
        elif action == 'sell':
            qty = s['position_qty']
            if self.api.mode == 'paper':
                received = qty * price * (1 - FEE_ESTIMATE)
                s['paper_cash'] += received
                s['paper_btc'] = max(0.0, s['paper_btc'] - qty)
                s['realized_eur'] += received - qty * s['entry']
                s['position_qty'] = s['entry'] = s['high'] = 0
                self.store.log(f'ДЕМО ПРОДАВА {qty:.8f} BTC за {received:.2f} EUR', price)
            else:
                ins = self.api.instrument()
                available = balances['BTC']
                qty = min(qty, available)
                qty_txt = dformat(qty * 0.9999, ins['lotSz'])  # safety margin for fees / rounding
                if Decimal(qty_txt) < Decimal(ins['minSz']):
                    s['status'] = 'Твърде малко BTC за борсова продажба; проверете ръчно'
                    self.store.save()
                    return
                self._submit_safe('sell', qty_txt, 'base_ccy')
        s['last_trade'] = time.time() if self.api.mode == 'paper' else s['last_trade']
        s['trades'] += 1 if self.api.mode == 'paper' else 0
        s['last_error'] = ''
        self.store.save()

    def _submit_safe(self, side, sz, tgt):
        s = self.store.state
        cl_id = 'MISHO' + uuid.uuid4().hex[:20]
        # Save first. If network times out, resolve exactly this order; never blindly repeat.
        s['pending'] = {'side': side, 'clOrdId': cl_id, 'sz': sz, 'target': tgt, 'created': now_utc()}
        self.store.save()
        try:
            self.api.submit(side, sz, tgt, cl_id)
        except Exception as exc:
            s['halted'] = True  # unknown execution state: mandatory operator review
            self.store.log('СПРЯН: неясен резултат от поръчка. ' + str(exc)[:120])
            return
        try:
            self.reconcile()
        except Exception as exc:
            s['halted'] = True
            self.store.log('СПРЯН: необходима проверка на поръчката. ' + str(exc)[:120])


def loop(trader):
    while True:
        trader.tick()
        time.sleep(POLL_SECONDS)
