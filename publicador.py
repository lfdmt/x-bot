"""4) Publica no X os rascunhos aprovados: texto e mídia (imagem, gráfico ou GIF), sem link da matéria.

"Postar agora" publica direto do webhook do Telegram (publicar_um). "Em 30 min" e "Próximo horário
bom" gravam o horário em bot_estado (chave agendar:<id>) e este script, rodando a cada 5 minutos,
publica o que venceu. LIMITE_DIARIO (variável de ambiente) limita os posts por dia; 0 desliga o limite.
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
# Picos de engajamento no Brasil: manhã, almoço, fim de tarde e noite (horário de Brasília).
HORARIOS_BONS = [(8, 30), (12, 15), (18, 30), (20, 30)]
LIMITE_DIARIO = int(os.getenv("LIMITE_DIARIO", "3"))  # 0 = sem limite. 3: menos posts, mais voz própria


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


def posts_no_dia(db, quando: datetime) -> int:
    """Publicados mais agendados no dia (horário de Brasília) que contém `quando`."""
    inicio = quando.astimezone(BRASILIA).replace(hour=0, minute=0, second=0, microsecond=0)
    fim = inicio + timedelta(days=1)
    publicados = (db.table("rascunhos").select("id").eq("status", "publicado")
                  .gte("publicado_em", inicio.isoformat()).lt("publicado_em", fim.isoformat()).execute().data)
    agendados = [h for h in agendamentos(db).values() if inicio <= h < fim]
    return len(publicados) + len(agendados)


def dia_cheio(db, quando: datetime) -> bool:
    return LIMITE_DIARIO > 0 and posts_no_dia(db, quando) >= LIMITE_DIARIO


def proximo_horario_bom(db) -> datetime | None:
    """Próximo horário de pico livre, em um dia que ainda não bateu o limite."""
    agora = datetime.now(timezone.utc)
    ocupados = [h for h in agendamentos(db).values() if h.year < 9999]
    hoje = agora.astimezone(BRASILIA).date()
    for dias in range(8):
        dia = hoje + timedelta(days=dias)
        for hora, minuto in HORARIOS_BONS:
            horario = datetime(dia.year, dia.month, dia.day, hora, minuto, tzinfo=BRASILIA)
            if horario <= agora + timedelta(minutes=2):
                continue
            if any(abs(horario - h) < timedelta(minutes=45) for h in ocupados):
                continue
            if not dia_cheio(db, horario):
                return horario
    return None


def lembrar_de_postar(db, r: dict) -> None:
    """Modo grátis: em vez de postar pela API (paga), manda o tweet com o botão que abre o X já escrito."""
    from telegram_bot import CHAT_ID, manual, tg

    db.table("rascunhos").update({"status": "pendente"}).eq("id", r["id"]).execute()
    db.table("bot_estado").delete().eq("chave", f"agendar:{r['id']}").execute()
    tg("sendMessage", chat_id=CHAT_ID, disable_web_page_preview=True,
       text=f"⏰ Hora de postar o rascunho #{r['id']}:\n\n{r['texto']}"
            + ("\n\n(Tem imagem: salve a do rascunho e anexe no X.)" if r.get("midia_urls") else ""),
       reply_markup={"inline_keyboard": [manual(r)]})
    return None


def publicar_um(db, r: dict) -> str | None:
    """Publica um rascunho. Devolve o link do tweet, ou None se falhou ou se está em modo de teste."""
    from config import x_gratis
    if x_gratis():
        return lembrar_de_postar(db, r)
    if DRY_RUN:
        print(f"[publicador] MODO TESTE, não postado:\n  tweet: {r['texto']}\n"
              f"  mídia: {', '.join(r.get('midia_urls') or []) or 'nenhuma'}")
        return None

    # Trava: se outro processo (Vercel, GitHub, Mac) já está publicando este rascunho, não repete.
    try:
        db.table("bot_estado").insert({"chave": f"publicando:{r['id']}", "valor": datetime.now(timezone.utc).isoformat()}).execute()
    except Exception:
        print(f"[publicador] #{r['id']} já está sendo publicado em outro lugar")
        return None
    atual = db.table("rascunhos").select("status").eq("id", r["id"]).execute().data
    if not atual or atual[0]["status"] != "aprovado":
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
