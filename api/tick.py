"""Relógio do bot (Vercel), chamado a cada 5 minutos pelo cron-job.org.

Substitui o agendamento do GitHub, que pode atrasar ou nem disparar: publica o que venceu, envia
rascunhos novos ao Telegram e dispara no GitHub a coleta (a cada 3 h) e os pedidos feitos pelo chat.
"""
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import publicador
from config import supabase
from publicador import BRASILIA
from respostas import descobrir, pedidos, pedidos_de_tweet
from telegram_bot import disparar, enviar_pendentes

INTERVALO_COLETA = timedelta(hours=3)
INTERVALO_PEDIDOS = timedelta(minutes=10)
# Posts em alta das contas grandes, de hora em hora nos picos (horário de Brasília).
HORAS_EM_ALTA = {int(h) for h in os.getenv("HORAS_EM_ALTA", "8,9,12,13,18,19,20,21").split(",") if h}


def vencido(db, chave: str, intervalo: timedelta) -> bool:
    """True (e marca agora) se a última vez foi há mais que `intervalo`."""
    agora = datetime.now(timezone.utc)
    linha = db.table("bot_estado").select("valor").eq("chave", chave).execute().data
    if linha and agora - datetime.fromisoformat(linha[0]["valor"]) < intervalo:
        return False
    db.table("bot_estado").upsert({"chave": chave, "valor": agora.isoformat()}).execute()
    return True


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        chave = parse_qs(urlparse(self.path).query).get("chave", [""])[0]
        if chave != os.environ["TELEGRAM_WEBHOOK_SECRET"]:
            self.send_response(403)
            self.end_headers()
            return
        db = supabase()
        # Batimento: mostra no banco quando o relógio chamou pela última vez.
        db.table("bot_estado").upsert({"chave": "tick:ultimo",
                                       "valor": datetime.now(timezone.utc).isoformat()}).execute()
        feito = []
        publicador.main()
        enviar_pendentes(db)
        if vencido(db, "tick:coleta", INTERVALO_COLETA):
            disparar("coletar-gerar.yml")
            feito.append("coleta")
        if (datetime.now(BRASILIA).hour in HORAS_EM_ALTA
                and vencido(db, "tick:em_alta", timedelta(minutes=55))):
            descobrir(db)
            feito.append("em_alta")
        # Rede de segurança: pedidos que o webhook não conseguiu disparar.
        if (pedidos(db) or pedidos_de_tweet(db)) and vencido(db, "tick:pedidos", INTERVALO_PEDIDOS):
            disparar("respostas.yml")
            feito.append("pedidos")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(("ok " + " ".join(feito)).encode())
