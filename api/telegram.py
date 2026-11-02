"""Webhook do Telegram (Vercel): processa cliques e respostas no instante em que acontecem."""
import json
import os
from http.server import BaseHTTPRequestHandler

from config import supabase
from telegram_bot import tratar_atualizacao


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # O Telegram manda este cabeçalho com o segredo definido no setWebhook.
        if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != os.environ["TELEGRAM_WEBHOOK_SECRET"]:
            self.send_response(403)
            self.end_headers()
            return
        tamanho = int(self.headers.get("Content-Length", 0))
        update = json.loads(self.rfile.read(tamanho) or b"{}")
        try:
            tratar_atualizacao(supabase(), update)
        finally:
            # Sempre 200: senão o Telegram reenvia o mesmo clique várias vezes.
            self.send_response(200)
            self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
