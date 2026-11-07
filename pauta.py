"""Assuntos do momento no Brasil, em português, para o /atualidades.

A API do X não entrega a aba de notícias do Brasil, e a lista de assuntos do momento dela vem cheia de
nomes soltos de celebridades. Então a lista sai das manchetes das últimas horas nos principais portais
brasileiros, agrupadas pelo Claude em assuntos (ex.: "teto de juros", "Selic", "Gleisi" e "Lara Resende"
viram um assunto só). Quando você escolhe um, o gerador lê os posts em português no X sobre ele, para o
tweet entrar na conversa de lá. Roda no GitHub Actions (o Claude está lá).
"""
import re
from datetime import datetime, timedelta, timezone
from time import mktime

import feedparser
import requests

PORTAIS = [
    ("g1", "https://g1.globo.com/rss/g1/"),
    ("Metrópoles", "https://www.metropoles.com/feed"),
    ("g1 Economia", "https://g1.globo.com/rss/g1/economia/"),
    ("g1 Política", "https://g1.globo.com/rss/g1/politica/"),
    ("InfoMoney", "https://www.infomoney.com.br/feed/"),
    ("CNN Brasil", "https://www.cnnbrasil.com.br/feed/"),
    ("Poder360", "https://www.poder360.com.br/feed/"),
    ("Folha", "https://feeds.folha.uol.com.br/emcimadahora/rss091.xml"),
]
JANELA = timedelta(hours=12)
def manchetes() -> list[dict]:
    limite = datetime.now(timezone.utc) - JANELA
    itens = []
    for portal, url in PORTAIS:
        try:
            feed = feedparser.parse(requests.get(url, headers={"User-Agent": "Mozilla/5.0 (x-bot)"}, timeout=15).content)
        except requests.RequestException:
            continue
        for e in feed.entries[:40]:
            data = e.get("published_parsed") or e.get("updated_parsed")
            if data and datetime.fromtimestamp(mktime(data), tz=timezone.utc) < limite:
                continue
            resumo = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            itens.append({"portal": portal, "titulo": e.title.strip(), "link": e.get("link", ""),
                          "resumo": re.sub(r"\s+", " ", resumo).strip()[:600]})
    return itens


# O que está rolando no dia, para o /viral: esporte, cultura, tecnologia e negócios. RSS, sem custo.
DIA_A_DIA = [
    ("ge F1", "https://ge.globo.com/rss/ge/motor/formula-1/", 8),
    ("ge", "https://ge.globo.com/rss/ge/", 10),
    ("g1", "https://g1.globo.com/rss/g1/", 10),
    ("g1 Pop", "https://g1.globo.com/rss/g1/pop-arte/", 6),
    ("g1 Tecnologia", "https://g1.globo.com/rss/g1/tecnologia/", 6),
    ("NeoFeed", "https://neofeed.com.br/feed/", 6),
    ("Brazil Journal", "https://braziljournal.com/feed/", 4),
    ("Exame", "https://exame.com/feed/", 8),
    ("TechCrunch", "https://techcrunch.com/feed/", 8),
    ("The Verge", "https://www.theverge.com/rss/index.xml", 6),
]


def manchetes_do_dia(horas: int = 18) -> str:
    """Manchetes recentes de esporte, cultura, tecnologia e negócios, uma por linha."""
    limite = datetime.now(timezone.utc) - timedelta(hours=horas)
    linhas = []
    for portal, url, maximo in DIA_A_DIA:
        try:
            feed = feedparser.parse(requests.get(url, headers={"User-Agent": "Mozilla/5.0 (x-bot)"}, timeout=15).content)
        except requests.RequestException:
            continue
        n = 0
        for e in feed.entries:
            data = e.get("published_parsed") or e.get("updated_parsed")
            if not data or datetime.fromtimestamp(mktime(data), tz=timezone.utc) < limite:
                continue
            linhas.append(f"[{portal}] {e.title.strip()}")
            n += 1
            if n == maximo:
                break
    return "\n".join(linhas)


SCHEMA_ASSUNTOS = {
    "type": "object",
    "properties": {"assuntos": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "titulo": {"type": "string", "description": "nome curto do assunto, em português"},
            "resumo": {"type": "string", "description": "uma frase com o que se sabe, só a partir das manchetes"},
            "busca_x": {"type": "string", "description": "2 a 4 palavras em português para buscar posts no X"},
            "manchetes": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["titulo", "resumo", "busca_x", "manchetes"],
        "additionalProperties": False,
    }}},
    "required": ["assuntos"],
    "additionalProperties": False,
}


def assuntos_agrupados(maximo: int = 10) -> list[dict]:
    """Os assuntos do dia no Brasil, agrupados e ordenados pelo Claude a partir das manchetes."""
    from gerador import claude_json

    itens = manchetes()
    lista = "\n".join(f"{n}. [{i['portal']}] {i['titulo']}" for n, i in enumerate(itens))
    saida = claude_json(
        f"Headlines from Brazilian news sites in the last hours:\n{lista}\n\n"
        f"Group them into the {maximo} topics Brazilians are discussing most right now. Rank by how many "
        "different outlets cover the topic, favouring politics, economy, markets, interest rates, crypto and "
        "tech over celebrity, weather or crime. Merge headlines about the same debate even if they use "
        "different words (e.g. a rate cap proposal, the Selic and a politician's comment on it). For each "
        "topic give the indexes of its headlines.",
        "You are a Brazilian news editor. Write titulo, resumo and busca_x in Brazilian Portuguese. "
        "resumo must only use what the headlines say.",
        SCHEMA_ASSUNTOS,
    )
    assuntos = []
    for a in (saida or {}).get("assuntos", [])[:maximo]:
        fontes = [itens[n] for n in a["manchetes"] if 0 <= n < len(itens)]
        if not fontes:
            continue
        assuntos.append({
            "name": a["titulo"], "summary": a["resumo"], "busca_x": a["busca_x"],
            "portais": len({f["portal"] for f in fontes}),
            "fontes": "\n".join(f"[{f['portal']}] {f['titulo']}: {f['resumo'][:300]}" for f in fontes[:6]),
        })
    return assuntos
