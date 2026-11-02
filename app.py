"""Zero-dependency iPhone-friendly dashboard and automatic bot server."""
from __future__ import annotations
import base64
import html
import hmac
import json
import os
import secrets
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from bot import Store, OKX, Trader, loop

CSS = '''
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#0b1020;color:#f2f6ff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
main{max-width:850px;margin:auto;padding:24px 16px 45px}.top{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:24px}
h1{font-size:24px;margin:0}.muted{color:#9daec4;font-size:13px}.tag{background:#213a52;border:1px solid #385e73;padding:8px 10px;border-radius:100px;font-size:12px}
.banner{padding:16px;background:#172338;border:1px solid #31445a;border-radius:14px;margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.tile{padding:18px;background:#151d30;border:1px solid #27334b;border-radius:16px;min-width:0}
.label{font-size:13px;color:#9daec4}.value{font-size:24px;font-weight:750;margin-top:8px;overflow-wrap:anywhere}.wide{grid-column:span 2}.good{color:#58dfad}.warning{color:#ffce79}
form{display:inline-block;margin:8px 8px 0 0}button{border:0;border-radius:10px;background:#e6efff;color:#132035;font-weight:650;padding:12px 18px;cursor:pointer}
.note{color:#afbed2;font-size:13px;line-height:1.55}li{padding:8px 0;border-bottom:1px solid #24314a;list-style:none}ul{padding:0}
@media(max-width:420px){.value{font-size:21px}h1{font-size:22px}}
'''


def e(x):
    return html.escape(str(x), quote=True)


def page(state, token):
    mode = state['mode']
    mode_label = {'paper': 'ДЕМО · Виртуални €', 'okx_demo': 'OKX Демо', 'live': 'РЕАЛНИ ПАРИ'}[mode]
    warnings = ''
    if state.get('halted'):
        warnings += '<div class="banner warning"><b>ЗАЩИТНО СПИРАНЕ.</b> Неизяснена борсова поръчка — нужна ръчна проверка.</div>'
    if state.get('last_error'):
        warnings += '<div class="banner warning">Грешка: ' + e(state['last_error']) + '</div>'
    history = ''.join('<li><b>' + e(t['message']) + '</b><div class="muted">' + e(t['time']) + ' UTC</div></li>' for t in state.get('history', []))
    if not history:
        history = '<li class="muted">Още няма сделки. Ботът изчаква пазарен сигнал.</li>'
    toggle = 'Продължи' if state['paused'] else 'Пауза'
    return f'''<!doctype html><html lang="bg"><head><meta name="viewport" content="width=device-width, initial-scale=1">
<meta charset="utf-8"><meta http-equiv="refresh" content="90"><title>Мишо AutoBot</title><style>{CSS}</style></head>
<body><main><div class="top"><div><h1>Мишо AutoBot</h1><span class="muted">BTC / EUR · Автоматична спот търговия</span></div>
<span class="tag">{e(mode_label)}</span></div>{warnings}<div class="grid">
<div class="tile"><div class="label">Оценка на сметката</div><div class="value">{state['total_value']:.2f} €</div></div>
<div class="tile"><div class="label">Цена на Bitcoin</div><div class="value">{state['last_price']:.2f} €</div></div>
<div class="tile"><div class="label">Позиция</div><div class="value">{state['position_qty']:.6f} BTC</div></div>
<div class="tile"><div class="label">Реализиран резултат*</div><div class="value">{state['realized_eur']:+.2f} €</div></div>
<div class="tile wide"><div class="label">Какво прави в момента</div><div class="value" style="font-size:20px">{e(state['status'])}</div>
<p class="muted">{e(state['last_signal'])}</p><p class="muted">Последна проверка: {e(state['last_check'] or 'няма')} (UTC)</p>
<form action="/toggle" method="post"><input type="hidden" name="csrf" value="{e(token)}"><button>{toggle}</button></form></div></div>
<h2 style="margin-top:28px;font-size:19px">Последни сделки</h2><ul>{history}</ul>
<p class="note">*Реализираният резултат при реален режим е приблизителен и не включва пълно отчитане на такси и данъци. Демо режимът ползва примерна такса. Ботът следва правила, а не самообучаващ се AI. Няма гарантирана печалба. Стоп-защитите не гарантират цена при срив, загубена връзка или волатилност. Никога не разрешавай тегления чрез API ключове.</p>
</main></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def _auth(self):
        pw = os.getenv('BOT_PASSWORD', '')
        if not pw:
            return True
        header = self.headers.get('Authorization', '')
        if header.startswith('Basic '):
            try:
                plain = base64.b64decode(header[6:], validate=True).decode('utf8')
                _, offered_password = plain.split(':', 1)
                if hmac.compare_digest(offered_password, pw):
                    return True
            except (ValueError, UnicodeError):
                pass
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Misho AutoBot"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def _response(self, code, content, content_type='text/html; charset=utf-8', headers=None):
        data = content.encode('utf8')
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            self._response(200, json.dumps({'ok': True}), 'application/json; charset=utf-8')
            return
        if path != '/':
            self._response(404, 'Not found')
            return
        if not self._auth():
            return
        store = self.server.store
        with store.lock:
            view = dict(store.state)
            view['history'] = list(store.state['history'])
        self._response(200, page(view, self.server.csrf))

    def do_POST(self):
        if urlsplit(self.path).path != '/toggle':
            self._response(404, 'Not found')
            return
        if not self._auth():
            return
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            self._response(403, 'Invalid origin')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 4096:
                raise ValueError('Invalid body size')
            values = parse_qs(self.rfile.read(length).decode('utf8'))
            token = values.get('csrf', [''])[0]
            if not hmac.compare_digest(token, self.server.csrf):
                raise ValueError('Invalid token')
        except (ValueError, UnicodeError):
            self._response(403, 'Invalid form')
            return
        store = self.server.store
        with store.lock:
            if store.state['halted']:
                self._response(409, 'Bot is halted after an uncertain order; manual review is required.')
                return
            store.state['paused'] = not store.state['paused']
            store.save()
        self._response(303, '', headers={'Location': '/'})

    def log_message(self, fmt, *args):
        # Avoid logging browser Authorization headers or other identifying data.
        return


def main():
    mode = os.getenv('MODE', 'paper').strip().lower()
    if mode not in ('paper', 'okx_demo', 'live'):
        raise RuntimeError('MODE must be paper, okx_demo or live.')
    if (mode != 'paper' or os.getenv('PUBLIC_DEPLOYMENT') == '1') and not os.getenv('BOT_PASSWORD'):
        raise RuntimeError('Set BOT_PASSWORD before starting a public dashboard or connecting an exchange.')
    store = Store(os.getenv('DATA_DIR', './data'))
    api = OKX(mode)
    trader = Trader(store, api)
    http = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '8080'))), Handler)
    http.daemon_threads = True
    http.store = store
    http.csrf = secrets.token_urlsafe(32)
    threading.Thread(target=loop, args=(trader,), daemon=True).start()
    print(f'Misho AutoBot {mode}: dashboard listening on port {http.server_port}', flush=True)
    http.serve_forever(poll_interval=0.5)


if __name__ == '__main__':
    main()
