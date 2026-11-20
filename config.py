"""Configuração compartilhada: variáveis de ambiente, Supabase e modo de teste."""
import os
import sys

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

# (origem, idioma, url). Os pilares e pesos ficam em temas.json; a curadoria decide o pilar de cada matéria.
FEEDS = [
    # Inovação, IA e Vale do Silício (o pilar principal)
    ("TechCrunch", "en", "https://techcrunch.com/feed/"),
    ("TechCrunch AI", "en", "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("TechCrunch Venture", "en", "https://techcrunch.com/category/venture/feed/"),
    ("The Information", "en", "https://www.theinformation.com/feed"),
    ("The Verge", "en", "https://www.theverge.com/rss/index.xml"),
    ("Bloomberg Tech", "en", "https://feeds.bloomberg.com/technology/news.rss"),
    ("VentureBeat", "en", "https://venturebeat.com/feed/"),
    ("Crunchbase News", "en", "https://news.crunchbase.com/feed/"),
    ("Sifted", "en", "https://sifted.eu/feed/"),
    ("Rest of World", "en", "https://restofworld.org/feed/latest/"),
    ("Neofeed", "pt", "https://neofeed.com.br/feed/"),
    ("Startups.com.br", "pt", "https://startups.com.br/feed/"),
    ("Startupi", "pt", "https://startupi.com.br/feed/"),
    ("MIT Tech Review Brasil", "pt", "https://mittechreview.com.br/feed/"),
    ("Mobile Time", "pt", "https://www.mobiletime.com.br/feed/"),
    # Fintech e cripto (produto, não preço)
    ("TechCrunch Fintech", "en", "https://techcrunch.com/category/fintech/feed/"),
    ("Fintechs Brasil", "pt", "https://fintechs.com.br/feed/"),
    ("BeInCrypto Brasil", "pt", "https://br.beincrypto.com/feed/"),
    ("CoinDesk", "en", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    # Comunicação e reputação
    ("Meio & Mensagem", "pt", "https://www.meioemensagem.com.br/feed"),
    ("B9", "pt", "https://www.b9.com.br/feed/"),
    ("Adweek", "en", "https://www.adweek.com/feed/"),
    ("Fast Company", "en", "https://www.fastcompany.com/latest/rss"),
]

# Pautas de preço do CoinGecko (BTC subiu X% etc.). Desligadas desde 2026-11-20: o foco é inovação.
COLETAR_MERCADO = os.getenv("COLETAR_MERCADO", "false").lower() == "true"

# Perfis do X lidos como pauta (cada post lido custa cerca de US$ 0,005 na API do X).
PERFIS_X = {"beincrypto": "1021643080208801792"}

# Ativos monitorados: id no CoinGecko -> (nome, cashtag, variação mínima em 24h para virar pauta)
ATIVOS = {
    "bitcoin": ("Bitcoin", "$BTC", 5.0),
    "ethereum": ("Ethereum", "$ETH", 6.0),
    "solana": ("Solana", "$SOL", 8.0),
    "ripple": ("XRP", "$XRP", 8.0),
}


def x_gratis() -> bool:
    """Modo grátis (padrão): nada chama a API paga do X. Para religar, X_GRATIS=false."""
    return os.getenv("X_GRATIS", "true").lower() != "false"


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
