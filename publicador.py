"""4) Publica no X os rascunhos aprovados: texto e mídia (imagem, gráfico ou GIF), sem link da matéria.

Com DRY_RUN=true (padrão) só mostra o que seria postado e não altera nada.
"""
import os
from datetime import datetime, timezone

import requests
import tweepy

from config import DRY_RUN, env, supabase
from midia import enviar_para_x

POR_EXECUCAO = int(os.getenv("PUBLICACOES_POR_EXECUCAO", "1"))


def avisar_telegram(texto: str):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": texto, "disable_web_page_preview": True},
                      timeout=20)


def cliente_x() -> tweepy.Client:
    return tweepy.Client(
        consumer_key=env("X_API_KEY"),
        consumer_secret=env("X_API_SECRET"),
        access_token=env("X_ACCESS_TOKEN"),
        access_token_secret=env("X_ACCESS_TOKEN_SECRET"),
    )


def main():
    db = supabase()
    fila = (
        db.table("rascunhos").select("*").eq("status", "aprovado")
        .order("criado_em").limit(POR_EXECUCAO).execute().data
    )
    if not fila:
        print("[publicador] nada aprovado na fila")
        return

    if DRY_RUN:
        for r in fila:
            print(f"[publicador] MODO TESTE, não postado:\n  tweet: {r['texto']}\n  mídia: {', '.join(r.get('midia_urls') or []) or 'nenhuma'}")
        return

    x = cliente_x()
    for r in fila:
        media_ids = None
        if r.get("midia_urls"):
            try:
                media_ids = [enviar_para_x(url) for url in r["midia_urls"][:4]]
            except Exception as e:  # sem mídia é melhor que não postar
                avisar_telegram(f"⚠️ Rascunho #{r['id']}: a mídia falhou ({e}). Publicando só o texto.")
        try:
            tweet = x.create_tweet(text=r["texto"], media_ids=media_ids)
            tweet_id = tweet.data["id"]
        except tweepy.TweepyException as e:
            db.table("rascunhos").update({"status": "erro", "erro": str(e)[:500]}).eq("id", r["id"]).execute()
            avisar_telegram(f"⚠️ Falha ao publicar o rascunho #{r['id']}: {e}")
            continue

        db.table("rascunhos").update({
            "status": "publicado",
            "tweet_id": tweet_id,
            "publicado_em": datetime.now(timezone.utc).isoformat(),
        }).eq("id", r["id"]).execute()

        avisar_telegram(f"🚀 Publicado: https://x.com/i/status/{tweet_id}")
        print(f"[publicador] publicado {tweet_id}")


if __name__ == "__main__":
    main()
