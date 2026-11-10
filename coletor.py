"""1) Coleta pautas e salva na tabela "fontes":
   - feeds RSS (BeInCrypto PT/ES e outros sites cripto)
   - movimentos fortes de mercado (CoinGecko) e medo e ganância extremos
   - posts recentes do @beincrypto no X
"""
import html
import os
import re
from datetime import datetime, timezone
from time import mktime

import feedparser
import requests
import tweepy

from config import ATIVOS, FEEDS, PERFIS_X, br, supabase

UA = {"User-Agent": "Mozilla/5.0 (x-bot)"}


def limpar(texto: str) -> str:
    texto = re.sub(r"<[^>]+>", " ", texto or "")
    texto = re.sub(r"\s+", " ", html.unescape(texto)).strip()
    # Rodapé automático dos feeds ("O artigo X foi visto pela primeira vez em..." / "El post X fue visto...")
    return re.sub(r"\s*(O artigo|El artículo|El post) .*(primeira|primera) vez e[mn] .*$", "", texto)


def imagem_do_item(item) -> str | None:
    for campo in ("media_content", "media_thumbnail"):
        for m in item.get(campo) or []:
            if m.get("url"):
                return m["url"]
    for e in item.get("enclosures") or []:
        if e.get("type", "").startswith("image/"):
            return e.get("href")
    return None


def coletar_feeds() -> list[dict]:
    linhas = []
    for origem, idioma, url in FEEDS:
        try:
            r = requests.get(url, headers=UA, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"[coletor] falha ao baixar {url}: {e}")
            continue
        feed = feedparser.parse(r.content)
        if feed.bozo and not feed.entries:
            print(f"[coletor] falha ao ler {url}: {feed.bozo_exception}")
            continue
        for item in feed.entries:
            publicado = item.get("published_parsed")
            linhas.append({
                "url": item.link,
                "titulo": limpar(item.title),
                "resumo": limpar(item.get("summary", ""))[:1500],
                "idioma": idioma,
                "tipo": "noticia",
                "origem": origem,
                "imagem_url": imagem_do_item(item),
                "publicado_em": (
                    datetime.fromtimestamp(mktime(publicado), tz=timezone.utc).isoformat()
                    if publicado else None
                ),
            })
        print(f"[coletor] {origem}: {len(feed.entries)} itens")
    return linhas


def coletar_mercado() -> list[dict]:
    agora = datetime.now(timezone.utc)
    hoje = agora.date().isoformat()
    linhas = []
    try:
        precos = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ",".join(ATIVOS), "vs_currencies": "usd", "include_24hr_change": "true"},
            headers=UA, timeout=30,
        ).json()
    except (requests.RequestException, ValueError) as e:
        print(f"[coletor] CoinGecko indisponível: {e}")
        precos = {}

    for moeda, (nome, cashtag, minimo) in ATIVOS.items():
        p = precos.get(moeda)
        if not p or abs(p.get("usd_24h_change") or 0) < minimo:
            continue
        variacao, preco = p["usd_24h_change"], p["usd"]
        try:
            serie = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{moeda}/market_chart",
                params={"vs_currency": "usd", "days": 7}, headers=UA, timeout=30,
            ).json()["prices"][::6]  # um ponto a cada 6 horas basta para o gráfico
        except (requests.RequestException, ValueError, KeyError):
            serie = []
        verbo = "sobe" if variacao > 0 else "cai"
        linhas.append({
            # Uma pauta por ativo por dia, mesmo que o movimento continue.
            "url": f"mercado:{moeda}:{hoje}",
            "titulo": f"{nome} ({cashtag}) {verbo} {br(abs(variacao))}% em 24h e vale US$ {br(preco, 0)}",
            "resumo": f"Dados CoinGecko em {agora:%d/%m/%Y %H:%M} UTC. Variação de 24h: {'+' if variacao > 0 else ''}{br(variacao, 2)}%. Preço: US$ {br(preco, 2)}.",
            "idioma": "pt",
            "tipo": "mercado",
            "origem": "CoinGecko",
            "dados": {"moeda": moeda, "nome": nome, "cashtag": cashtag, "variacao": variacao,
                      "preco": preco, "serie": serie},
            "publicado_em": agora.isoformat(),
        })

    try:
        fng = requests.get("https://api.alternative.me/fng/?limit=1", headers=UA, timeout=30).json()["data"][0]
        valor = int(fng["value"])
        if valor <= 20 or valor >= 80:
            estado = "medo extremo" if valor <= 20 else "ganância extrema"
            linhas.append({
                "url": f"mercado:medo-ganancia:{hoje}",
                "titulo": f"Índice de medo e ganância das criptos marca {valor} ({estado})",
                "resumo": f"Crypto Fear & Greed Index (alternative.me) em {valor} de 100, classificado como {fng['value_classification']}.",
                "idioma": "pt",
                "tipo": "mercado",
                "origem": "alternative.me",
                "dados": {"indice": valor},
                "publicado_em": agora.isoformat(),
            })
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        print(f"[coletor] índice de medo e ganância indisponível: {e}")

    print(f"[coletor] mercado: {len(linhas)} pautas")
    return linhas


def coletar_x(db) -> list[dict]:
    if os.getenv("COLETAR_X", "false").lower() != "true" or not os.getenv("X_ACCESS_TOKEN"):
        return []
    from publicador import cliente_x

    x = cliente_x()
    linhas = []
    for usuario, user_id in PERFIS_X.items():
        chave = f"x_since_id_{usuario}"
        estado = db.table("bot_estado").select("valor").eq("chave", chave).execute().data
        try:
            resp = x.get_users_tweets(
                user_id, max_results=10, user_auth=True,
                since_id=estado[0]["valor"] if estado else None,
                exclude=["replies", "retweets"],
                tweet_fields=["created_at", "attachments"],
                expansions=["attachments.media_keys"],
                media_fields=["type", "url", "preview_image_url"],
            )
        except tweepy.TweepyException as e:
            print(f"[coletor] falha ao ler @{usuario}: {e}")
            continue
        if not resp.data:
            continue
        midias = {m.media_key: (m.url or m.preview_image_url) for m in (resp.includes or {}).get("media", [])}
        for post in resp.data:
            chaves = (post.attachments or {}).get("media_keys", [])
            texto = re.sub(r"\s*https://t\.co/\S+", "", post.text).strip()
            linhas.append({
                "url": f"https://x.com/{usuario}/status/{post.id}",
                "titulo": texto.split("\n")[0][:200],
                "resumo": texto[:1500],
                "idioma": "en",
                "tipo": "x",
                "origem": f"@{usuario}",
                "imagem_url": next((midias[k] for k in chaves if midias.get(k)), None),
                "publicado_em": post.created_at.isoformat() if post.created_at else None,
            })
        db.table("bot_estado").upsert({"chave": chave, "valor": str(resp.meta["newest_id"])}).execute()
        print(f"[coletor] @{usuario}: {len(resp.data)} posts novos")
    return linhas


def main():
    db = supabase()
    linhas = coletar_feeds() + coletar_mercado() + coletar_x(db)
    if not linhas:
        return
    # O upsert em lote exige as mesmas colunas em todas as linhas.
    colunas = {c for linha in linhas for c in linha}
    linhas = [{c: linha.get(c) for c in colunas} for linha in linhas]
    # ignore_duplicates: URLs já salvas não são sobrescritas (nem voltam a "usado=false").
    db.table("fontes").upsert(linhas, on_conflict="url", ignore_duplicates=True).execute()
    print(f"[coletor] {len(linhas)} itens enviados ao Supabase (duplicados ignorados)")


if __name__ == "__main__":
    main()
