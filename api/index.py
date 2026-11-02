"""Ponto de entrada único na Vercel: /api/telegram (webhook) e /api/tick (relógio)."""
from http.server import BaseHTTPRequestHandler

from api.telegram import handler as Telegram
from api.tick import handler as Relogio


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/tick"):
            Relogio.do_GET(self)
        else:
            Telegram.do_GET(self)

    def do_POST(self):
        Telegram.do_POST(self)
