"""3) Bot de Telegram: postar agora, agendar para daqui a 30 minutos, editar ou descartar rascunhos.

Os cliques chegam na hora pelo webhook na Vercel (api/telegram.py). Este script, rodando
no GitHub Actions, envia os rascunhos novos. `python telegram_bot.py --loop` também processa
cliques por leitura, para uso local sem webhook.

Para editar: responda (reply) à mensagem do rascunho com o texto novo.
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from config import env, supabase
from publicador import (BRASILIA, EM_ANDAMENTO, LIMITE_DIARIO, agendar, dia_cheio,
                        proximo_horario_bom, publicar_um)

API = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}"
CHAT_ID = int(env("TELEGRAM_CHAT_ID"))
LIMITE_TWEET = 280


# Respostas curtas que significam "postar agora", nunca "trocar o texto por isto".
COMANDOS_POSTAR = {"pode postar", "postar", "posta", "postar agora", "agora", "aprovar", "aprovado",
                   "aprova", "ok", "sim"}
ESPERA_AGENDADA = timedelta(minutes=30)
NOMES_MIDIA = {"materia": "imagem da matéria", "grafico": "gráfico", "gif": "GIF", "logos": "cards com logos"}


def tg(metodo: str, silencioso: bool = False, **params):
    r = requests.post(f"{API}/{metodo}", json=params, timeout=40)
    dados = r.json()
    if not dados.get("ok") and not silencioso:
        print(f"[telegram] {metodo} falhou: {dados}")
    return dados.get("result")


def tg_arquivo(metodo: str, campo: str, url: str, **params):
    """Envia a mídia como arquivo: alguns sites bloqueiam o download direto feito pelo Telegram."""
    try:
        conteudo = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (x-bot)"}, timeout=60).content
    except requests.RequestException as e:
        print(f"[telegram] falha ao baixar {url}: {e}")
        return None
    dados = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
    r = requests.post(f"{API}/{metodo}", data=dados, files={campo: ("midia", conteudo)}, timeout=120)
    resultado = r.json()
    if not resultado.get("ok"):
        print(f"[telegram] {metodo} (arquivo) falhou: {resultado}")
    return resultado.get("result")


def editar(message_id: int, texto: str, markup: dict | None = None):
    """Edita o rascunho, seja mensagem de texto ou foto/GIF com legenda."""
    extra = {"reply_markup": markup} if markup else {}
    if tg("editMessageText", silencioso=True, chat_id=CHAT_ID, message_id=message_id,
          text=texto, disable_web_page_preview=True, **extra) is None:
        tg("editMessageCaption", chat_id=CHAT_ID, message_id=message_id, caption=texto, **extra)


def botoes(r: dict) -> dict:
    segunda = [{"text": "🗑 Descartar", "callback_data": f"descartar:{r['id']}"}]
    if r.get("midia_urls"):
        segunda.append({"text": "🚫 Sem mídia", "callback_data": f"semmidia:{r['id']}"})
    return {"inline_keyboard": [
        [{"text": "🚀 Postar agora", "callback_data": f"agora:{r['id']}"},
         {"text": "⏰ Em 30 min", "callback_data": f"em30:{r['id']}"}],
        [{"text": "🕐 Próximo horário bom", "callback_data": f"bom:{r['id']}"}],
        segunda,
    ]}


AVISO_LIMITE = (f"Limite de {LIMITE_DIARIO} posts por dia atingido. "
                "Use 🕐 Próximo horário bom para agendar no próximo dia livre.")


def repetido_assunto(db, message_id: int, i: int) -> bool:
    chave = f"alta_pedido:{message_id}:{i}"
    ja = db.table("bot_estado").select("chave").eq("chave", chave).execute().data
    db.table("bot_estado").upsert({"chave": chave, "valor": "1"}).execute()
    return bool(ja)


def mostrar_atualidades(db):
    """Pede ao GitHub (onde está o Claude) a lista de assuntos do momento no Brasil."""
    db.table("bot_estado").upsert({"chave": "pedido_atualidades", "valor": "1"}).execute()
    disparar("respostas.yml")
    tg("sendMessage", chat_id=CHAT_ID,
       text="⏳ Montando os assuntos do momento no Brasil. A lista chega em 2 a 3 minutos.")


def confirmar(message_id: int, texto: str):
    """Mensagem nova, respondendo ao rascunho: mais visível que só editar a mensagem original."""
    tg("sendMessage", silencioso=True, chat_id=CHAT_ID, reply_to_message_id=message_id, text=texto)


def repetido(db, cq: dict) -> bool:
    """Cliques repetidos no mesmo botão não geram uma enxurrada de avisos iguais."""
    chave = f"clique:{cq['message']['message_id']}"
    ja = db.table("bot_estado").select("valor").eq("chave", chave).execute().data
    db.table("bot_estado").upsert({"chave": chave, "valor": cq["data"]}).execute()
    return bool(ja)


def reservar(db, r: dict) -> bool:
    """Passa de pendente para aprovado só se ainda estiver pendente (evita postar duas vezes num duplo clique)."""
    return bool(db.table("rascunhos").update({"status": "aprovado"})
                .eq("id", r["id"]).eq("status", "pendente").execute().data)


def postar_agora(db, r: dict, message_id: int):
    if not reservar(db, r):
        return
    agendar(db, r["id"], EM_ANDAMENTO)  # o agendador do GitHub não pega este
    editar(message_id, f"{corpo(r)}\n\n⏳ Publicando…")
    link = publicar_um(db, r)  # quando dá certo, publicar_um já manda "🚀 Publicado: link"
    if link:
        editar(message_id, f"{corpo(r)}\n\n🚀 PUBLICADO: {link}")
    else:
        status = db.table("rascunhos").select("status").eq("id", r["id"]).execute().data[0]["status"]
        aviso = "⚠️ Falhou, veja a mensagem de erro" if status == "erro" else "🧪 Modo de teste: não foi postado"
        editar(message_id, f"{corpo(r)}\n\n{aviso}")
        confirmar(message_id, f"{aviso} (rascunho #{r['id']})")


def agendar_para(db, r: dict, message_id: int, quando: datetime):
    if not reservar(db, r):
        return
    agendar(db, r["id"], quando)
    local = quando.astimezone(BRASILIA)
    dia = "hoje" if local.date() == datetime.now(BRASILIA).date() else f"em {local:%d/%m}"
    editar(message_id, f"{corpo(r)}\n\n⏰ AGENDADO para {dia} às {local:%H:%M} (Brasília)")
    extra = ""
    if local.date() != datetime.now(BRASILIA).date() and dia_cheio(db, datetime.now(timezone.utc)):
        extra = f"\nHoje já tem {LIMITE_DIARIO} posts, por isso foi para o próximo dia."
    confirmar(message_id, f"⏰ Agendado: rascunho #{r['id']} sai {dia} às {local:%H:%M}.{extra}")


def corpo(r: dict) -> str:
    midia = NOMES_MIDIA.get(r.get("midia_tipo")) if r.get("midia_urls") else None
    return (f"📝 Rascunho #{r['id']} ({len(r['texto'])}/280)\n\n{r['texto']}\n\n"
            f"Mídia: {midia or 'nenhuma, sai só o texto'}\n"
            f"Fonte (só para você conferir, não vai no tweet): {r['link']}")


def ler_offset(db) -> int:
    linhas = db.table("bot_estado").select("valor").eq("chave", "telegram_offset").execute().data
    return int(linhas[0]["valor"]) if linhas else 0


def salvar_offset(db, offset: int):
    db.table("bot_estado").upsert({"chave": "telegram_offset", "valor": str(offset)}).execute()


AVISO_PEDIDO = "⏳ Recebi o link. As 3 opções de resposta chegam aqui em alguns minutos."
AJUDA = ("Comandos:\n"
         "• /gerar: busca notícias e gera rascunhos agora\n"
         "• /atualidades: assuntos do momento no Brasil, para tweetar sobre eles\n"
         "• /linkedin: post do LinkedIn com a melhor notícia do dia. /linkedin seguido de um tema usa a sua pauta\n"
         "• /exemplo seguido de um post seu do LinkedIn: o bot aprende o seu jeito de escrever\n"
         "• Escreva o que quer tweetar (ex.: \"tweet sobre o hack do Mercado Bitcoin e a confiança nas "
         "exchanges brasileiras\") e eu faço o rascunho\n"
         "• Cole o link de um post do X para receber 3 sugestões de resposta\n"
         "• Responda a um rascunho com outro texto para trocar o tweet")
REPOSITORIO = "lfdmt/x-bot"


def disparar(workflow: str):
    """Roda um workflow do GitHub na hora, sem esperar o agendamento."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return
    r = requests.post(f"https://api.github.com/repos/{REPOSITORIO}/actions/workflows/{workflow}/dispatches",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main"}, timeout=20)
    if r.status_code != 204:
        print(f"[telegram] não consegui disparar {workflow}: {r.status_code} {r.text[:200]}")


def tratar_clique(db, cq: dict):
    acao, _, rid = cq["data"].partition(":")
    msg = cq["message"]
    if acao == "alta":
        lista = db.table("bot_estado").select("valor").eq("chave", "atualidades").execute().data
        assuntos = json.loads(lista[0]["valor"]) if lista else []
        i = int(rid)
        if i >= len(assuntos):
            tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Lista antiga, mande /atualidades de novo")
            return
        escolhido = assuntos[i] if isinstance(assuntos[i], dict) else {"name": assuntos[i]}
        db.table("bot_estado").upsert({"chave": f"pedido_alta:{msg['message_id']}:{i}",
                                       "valor": json.dumps(escolhido, ensure_ascii=False)}).execute()
        if repetido_assunto(db, msg["message_id"], i):
            tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Já estou escrevendo esse")
            return
        disparar("respostas.yml")
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Escrevendo")
        confirmar(msg["message_id"], f"⏳ Escrevendo sobre \"{escolhido['name']}\". O rascunho chega em 2 a 3 minutos.")
        return
    if acao == "outras":
        from respostas import registrar_pedido
        tweet_id, _, usuario = rid.partition(":")
        registrar_pedido(db, f"x.com/{usuario}/status/{tweet_id}")
        disparar("respostas.yml")
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text=AVISO_PEDIDO, show_alert=True)
        return
    rascunho = db.table("rascunhos").select("*").eq("id", int(rid)).execute().data
    if not rascunho or rascunho[0]["status"] != "pendente":
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Esse rascunho não está mais pendente")
        if rascunho and not repetido(db, cq):
            confirmar(msg["message_id"], f"ℹ️ Rascunho #{rid} já está {rascunho[0]['status']}.")
        return
    r = rascunho[0]
    if acao == "semmidia":
        db.table("rascunhos").update({"midia_tipo": None, "midia_urls": None}).eq("id", r["id"]).execute()
        r["midia_tipo"] = r["midia_urls"] = None
        editar(msg["message_id"], corpo(r), botoes(r))
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Vai sair só o texto")
        confirmar(msg["message_id"], f"🚫 Rascunho #{r['id']} vai sair sem mídia, só o texto.")
        return
    agora = datetime.now(timezone.utc)
    if acao in ("agora", "aprovar", "em30") and dia_cheio(db, agora + (ESPERA_AGENDADA if acao == "em30" else timedelta())):
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text=AVISO_LIMITE, show_alert=True)
        confirmar(msg["message_id"], f"⛔ {AVISO_LIMITE}")
        return
    if acao in ("agora", "aprovar"):  # "aprovar" vem de mensagens antigas
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Publicando…")
        postar_agora(db, r, msg["message_id"])
        return
    if acao == "em30":
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Agendado")
        agendar_para(db, r, msg["message_id"], agora + ESPERA_AGENDADA)
        return
    if acao == "bom":
        quando = proximo_horario_bom(db)
        if not quando:
            tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"],
               text="Nenhum horário livre nos próximos 7 dias.", show_alert=True)
            return
        tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Agendado")
        agendar_para(db, r, msg["message_id"], quando)
        return
    db.table("rascunhos").update({"status": "descartado"}).eq("id", r["id"]).execute()
    editar(msg["message_id"], f"{corpo(r)}\n\n🗑 DESCARTADO")
    confirmar(msg["message_id"], f"🗑 Rascunho #{r['id']} descartado.")
    tg("answerCallbackQuery", silencioso=True, callback_query_id=cq["id"], text="Descartado")


def tratar_resposta(db, msg: dict):
    original = msg["reply_to_message"]["message_id"]
    linhas = db.table("rascunhos").select("*").eq("telegram_message_id", original).execute().data
    if not linhas:
        return
    r = linhas[0]
    if r["status"] != "pendente":
        tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
           text="Esse rascunho não está mais pendente, edição ignorada.")
        return
    texto = msg["text"].strip()
    if texto.lower().strip(" .!") in COMANDOS_POSTAR:
        if dia_cheio(db, datetime.now(timezone.utc)):
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"], text=AVISO_LIMITE)
            return
        postar_agora(db, r, original)
        return
    if len(texto) > LIMITE_TWEET:
        tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
           text=f"Texto com {len(texto)} caracteres. O limite é {LIMITE_TWEET}.")
        return
    db.table("rascunhos").update({"texto": texto}).eq("id", r["id"]).execute()
    r["texto"] = texto
    editar(original, corpo(r), botoes(r))
    tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=original, text="✏️ Texto atualizado")


def tratar_atualizacao(db, u: dict):
    """Um clique ou mensagem do Telegram. Usado pelo webhook (api/telegram.py) e pelo modo de leitura."""
    if "callback_query" in u:
        cq = u["callback_query"]
        if cq["message"]["chat"]["id"] == CHAT_ID:
            tratar_clique(db, cq)
    elif "message" in u:
        msg = u["message"]
        if msg["chat"]["id"] != CHAT_ID or not msg.get("text"):
            return
        if msg.get("reply_to_message"):
            tratar_resposta(db, msg)
            return
        from respostas import registrar_pedido
        texto = msg["text"].strip()
        if registrar_pedido(db, texto):
            disparar("respostas.yml")
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"], text=AVISO_PEDIDO)
        elif texto.lower().startswith("/atualidades"):
            mostrar_atualidades(db)
        elif texto.lower().startswith("/exemplo"):
            post = texto[len("/exemplo"):].strip()
            if len(post) < 80:
                tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
                   text="Mande /exemplo seguido do texto completo de um post seu do LinkedIn.")
                return
            db.table("bot_estado").upsert({"chave": f"linkedin_exemplo:{msg['message_id']}", "valor": post}).execute()
            total = len(db.table("bot_estado").select("chave").like("chave", "linkedin_exemplo:%").execute().data)
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
               text=f"✅ Guardei. Agora são {total} posts seus de exemplo (uso os 8 mais recentes).")
        elif texto.lower().startswith("/linkedin"):
            pauta = texto[len("/linkedin"):].strip()
            db.table("bot_estado").upsert({"chave": f"pedido_linkedin:{msg['message_id']}", "valor": pauta}).execute()
            disparar("respostas.yml")
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
               text="⏳ Escrevendo o post do LinkedIn. Chega aqui em 2 a 3 minutos.")
        elif texto.lower().startswith("/gerar"):
            disparar("coletar-gerar.yml")
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
               text="⏳ Buscando notícias. Os rascunhos novos chegam em alguns minutos.")
        elif len(texto) >= 15 and not texto.startswith("/"):
            db.table("bot_estado").upsert({"chave": f"pedido_tweet:{msg['message_id']}", "valor": texto}).execute()
            disparar("respostas.yml")
            tg("sendMessage", chat_id=CHAT_ID, reply_to_message_id=msg["message_id"],
               text="⏳ Escrevendo. O rascunho chega aqui em alguns minutos.")
        else:
            tg("sendMessage", chat_id=CHAT_ID, text=AJUDA)


def processar_atualizacoes(db, espera: int = 0):
    # Com o webhook ativo o Telegram recusa o getUpdates (409): os cliques já chegam pela Vercel.
    offset = ler_offset(db)
    updates = tg("getUpdates", silencioso=True, offset=offset, timeout=espera,
                 allowed_updates=["message", "callback_query"]) or []
    for u in updates:
        try:
            tratar_atualizacao(db, u)
        finally:
            offset = u["update_id"] + 1
            salvar_offset(db, offset)


def enviar_pendentes(db):
    novos = (
        db.table("rascunhos").select("*").eq("status", "pendente")
        .is_("telegram_message_id", "null").order("criado_em").execute().data
    )
    for r in novos:
        enviada = None
        urls = r.get("midia_urls") or []
        if len(urls) == 1:
            metodo, campo = ("sendAnimation", "animation") if r["midia_tipo"] == "gif" else ("sendPhoto", "photo")
            enviada = (tg(metodo, silencioso=True, chat_id=CHAT_ID, caption=corpo(r), reply_markup=botoes(r),
                          **{campo: urls[0]})
                       or tg_arquivo(metodo, campo, urls[0], chat_id=CHAT_ID, caption=corpo(r), reply_markup=botoes(r)))
        elif len(urls) > 1:
            # Álbum não aceita botões: manda as imagens e, em seguida, o texto com os botões.
            tg("sendMediaGroup", chat_id=CHAT_ID, media=[{"type": "photo", "media": u} for u in urls[:4]])
        if not enviada:
            enviada = tg("sendMessage", chat_id=CHAT_ID, text=corpo(r),
                         reply_markup=botoes(r), disable_web_page_preview=True)
        if enviada:
            db.table("rascunhos").update(
                {"telegram_message_id": enviada["message_id"]}
            ).eq("id", r["id"]).execute()
    if novos:
        print(f"[telegram] {len(novos)} rascunhos enviados")


def main():
    db = supabase()
    if "--loop" in sys.argv:
        print("[telegram] rodando em loop (Ctrl+C para sair)")
        while True:
            processar_atualizacoes(db, espera=30)
            enviar_pendentes(db)
            time.sleep(1)
    processar_atualizacoes(db)
    enviar_pendentes(db)


if __name__ == "__main__":
    main()
