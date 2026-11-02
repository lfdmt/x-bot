"""4) Publica no X os rascunhos aprovados: texto e mídia (imagem, gráfico ou GIF), sem link da matéria.

"Postar agora" publica direto do webhook do Telegram (publicar_um). "Em 30 min" grava o horário
em bot_estado (chave agendar:<id>) e este script, rodando a cada 5 minutos, publica o que venceu.
Com DRY_RUN=true só mostra o que seria postado e não altera nada.
"""
import os
from datetime import datetime, timedelta, timezone

import requests
import tweepy

from config import DRY_RUN, env, supabase
from midia import enviar_para_x

POR_EXECUCAO = int(os.getenv("PUBLICACOES_POR_EXECUCAO", "1"))
BRASILIA = timezone(timedelta(hours=-3))
# Marca de "publicação em andamento": impede que o agendador pegue o mesmo rascunho.
EM_ANDAMENTO = "9999-12-31T00:00:00+00:00"


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


def agendar(db, rascunho_id: int, quando: datetime | str):
    valor = quando if isinstance(quando, str) else quando.isoformat()
    db.table("bot_estado").upsert({"chave": f"agendar:{rascunho_id}", "valor": valor}).execute()


def agendamentos(db) -> dict[int, datetime]:
    linhas = db.table("bot_estado").select("*").like("chave", "agendar:%").execute().data
    return {int(l["chave"].split(":")[1]): datetime.fromisoformat(l["valor"]) for l in linhas}


def publicar_um(db, r: dict) -> str | None:
    """Publica um rascunho. Devolve o link do tweet, ou None se falhou ou se está em modo de teste."""
    if DRY_RUN:
        print(f"[publicador] MODO TESTE, não postado:\n  tweet: {r['texto']}\n"
              f"  mídia: {', '.join(r.get('midia_urls') or []) or 'nenhuma'}")
        return None

    media_ids = None
    if r.get("midia_urls"):
        try:
            media_ids = [enviar_para_x(url) for url in r["midia_urls"][:4]]
        except Exception as e:  # sem mídia é melhor que não postar
            avisar_telegram(f"⚠️ Rascunho #{r['id']}: a mídia falhou ({e}). Publicando só o texto.")
    try:
        tweet_id = cliente_x().create_tweet(text=r["texto"], media_ids=media_ids).data["id"]
    except tweepy.TweepyException as e:
        db.table("rascunhos").update({"status": "erro", "erro": str(e)[:500]}).eq("id", r["id"]).execute()
        db.table("bot_estado").delete().eq("chave", f"agendar:{r['id']}").execute()
        avisar_telegram(f"⚠️ Falha ao publicar o rascunho #{r['id']}: {e}")
        return None

    db.table("rascunhos").update({
        "status": "publicado",
        "tweet_id": tweet_id,
        "publicado_em": datetime.now(timezone.utc).isoformat(),
    }).eq("id", r["id"]).execute()
    db.table("bot_estado").delete().eq("chave", f"agendar:{r['id']}").execute()
    link = f"https://x.com/i/status/{tweet_id}"
    avisar_telegram(f"🚀 Publicado: {link}")
    print(f"[publicador] publicado {tweet_id}")
    return link


def main():
    db = supabase()
    agora = datetime.now(timezone.utc)
    horarios = agendamentos(db)
    aprovados = db.table("rascunhos").select("*").eq("status", "aprovado").order("criado_em").execute().data
    # Sem horário (aprovados antes dos botões novos) conta como "já pode".
    vencidos = [r for r in aprovados if horarios.get(r["id"], agora) <= agora][:POR_EXECUCAO]
    if not vencidos:
        print("[publicador] nada para publicar agora")
        return
    for r in vencidos:
        publicar_um(db, r)


if __name__ == "__main__":
    main()
