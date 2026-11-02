"""Configuração compartilhada: variáveis de ambiente, Supabase e modo de teste."""
import os
import sys

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

# (origem, idioma, url)
FEEDS = [
    ("BeInCrypto Brasil", "pt", "https://br.beincrypto.com/feed/"),
    ("BeInCrypto Español", "es", "https://es.beincrypto.com/feed/"),
    ("Livecoins", "pt", "https://livecoins.com.br/feed/"),
    ("Portal do Bitcoin", "pt", "https://portaldobitcoin.uol.com.br/feed/"),
    ("CoinDesk", "en", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "en", "https://cointelegraph.com/rss"),
    ("Decrypt", "en", "https://decrypt.co/feed"),
]

# Perfis do X lidos como pauta (cada post lido custa cerca de US$ 0,005 na API do X).
PERFIS_X = {"beincrypto": "1021643080208801792"}

# Ativos monitorados: id no CoinGecko -> (nome, cashtag, variação mínima em 24h para virar pauta)
ATIVOS = {
    "bitcoin": ("Bitcoin", "$BTC", 5.0),
    "ethereum": ("Ethereum", "$ETH", 6.0),
    "solana": ("Solana", "$SOL", 8.0),
    "ripple": ("XRP", "$XRP", 8.0),
}


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value == "":
        sys.exit(f"Variável de ambiente ausente: {name}")
    return value


# Modo de teste ligado por padrão: nada é postado no X até DRY_RUN=false.
DRY_RUN = os.getenv("DRY_RUN", "true").strip().lower() not in ("false", "0", "no", "nao")


def br(numero: float, casas: int = 1) -> str:
    """Formato brasileiro: 77.730,5"""
    return f"{numero:,.{casas}f}".replace(",", "_").replace(".", ",").replace("_", ".")


def supabase() -> Client:
    return create_client(env("SUPABASE_URL"), env("SUPABASE_SERVICE_KEY"))
