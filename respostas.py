"""5) Sugestões de resposta em português: o Claude escreve 3 respostas na sua voz e cada uma vira um
botão "Abrir no X", que abre o app com a resposta já escrita.

Dois caminhos:
- você cola no Telegram o link de um post: o webhook registra o pedido (bot_estado, chave
  pedido_resposta:<tweet_id>) e este script responde na rodada seguinte;
- `--descobrir`: busca os posts recentes de maior engajamento dos perfis em perfis_resposta.txt e
  sugere respostas para os melhores, antes dos horários de pico.

Roda no GitHub Actions, onde o Claude está disponível pela assinatura. `--checar` só diz se há
pedidos, para o workflow não instalar o Claude à toa.
"""
import html
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import tweepy

from config import supabase
from gerador import LIMITE_TWEET, VOZ, claude_json
from publicador import cliente_x
from telegram_bot import CHAT_ID, tg

PERFIS = Path(__file__).with_name("perfis_resposta.txt")
SUGESTOES_POR_RODADA = int(os.getenv("SUGESTOES_POR_RODADA", "3"))
JANELA = timedelta(hours=6)
LINK_POST = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]+)/status/(\d+)")

SCHEMA = {
    "type": "object",
    "properties": {
        "respostas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"angulo": {"type": "string"}, "texto": {"type": "string"}},
                "required": ["angulo", "texto"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["respostas"],
    "additionalProperties": False,
}


def registrar_pedido(db, texto: str) -> str | None:
    """Guarda o pedido se o texto tiver link de post do X. Devolve o id do post."""
    achado = LINK_POST.search(texto)
    if not achado:
        return None
    tweet_id = achado.group(2)
    db.table("bot_estado").upsert({"chave": f"pedido_resposta:{tweet_id}", "valor": achado.group(1)}).execute()
    return tweet_id


def pedidos(db) -> list[str]:
    linhas = db.table("bot_estado").select("chave").like("chave", "pedido_resposta:%").execute().data
    return [l["chave"].split(":", 1)[1] for l in linhas]


def sistema() -> str:
    return (
        "You write replies for the personal X account of the Head of LATAM at BeInCrypto. "
        "The voice guide below defines his tone; follow it.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        "Rules for every reply:\n"
        "- Write exactly 3 replies, each with a different angle: an insight or LATAM perspective, a "
        "respectful counterpoint, and a sharp question that invites the author to answer.\n"
        "- Always in Brazilian Portuguese, whatever the language of the original post.\n"
        f"- At most {LIMITE_TWEET - 20} characters each. No links, no hashtags, at most one emoji.\n"
        "- Use only facts stated in the post. Never bring outside numbers, names or events: offer "
        "opinion and angle instead.\n"
        "- Add something to the conversation. No empty praise ('great post'), no self promotion.\n"
        "- angulo is a 1 to 3 word label in Portuguese (e.g. 'Visão LATAM', 'Contraponto', 'Pergunta')."
    )


def responder(db, tweet_id: str, autor_link: str):
    try:
        resp = cliente_x().get_tweet(tweet_id, user_auth=True, expansions=["author_id"],
                                     tweet_fields=["note_tweet"], user_fields=["username"])
    except tweepy.TweepyException as e:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Não consegui ler o post {tweet_id}: {e}")
        return
    if not resp.data:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ O post {tweet_id} não existe mais ou é privado.")
        return
    autor = (resp.includes or {}).get("users", [None])[0]
    usuario = autor.username if autor else autor_link
    texto = (resp.data.data.get("note_tweet") or {}).get("text") or resp.data.text
    sugerir(tweet_id, usuario, texto)


def sugerir(tweet_id: str, usuario: str, texto: str, contexto: str = ""):
    saida = claude_json(f"Post by @{usuario}:\n\"\"\"\n{texto}\n\"\"\"\n\nWrite the 3 replies.", sistema(), SCHEMA)
    opcoes = [o for o in (saida or {}).get("respostas", []) if len(o["texto"]) <= LIMITE_TWEET][:3]
    if not opcoes:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Não saiu nenhuma resposta boa para o post de @{usuario}.")
        return

    resumo = texto if len(texto) <= 200 else texto[:200] + "…"
    post = f"https://x.com/{usuario}/status/{tweet_id}"
    linhas = [f"💬 Respostas para <a href=\"{post}\">@{html.escape(usuario)}</a>{html.escape(contexto)}:\n"
              f"<i>{html.escape(resumo)}</i>\n"]
    for i, o in enumerate(opcoes, 1):
        # Em <code>, um toque no texto copia a resposta inteira no Telegram.
        linhas.append(f"{i}️⃣ <b>{html.escape(o['angulo'])}</b>\n<code>{html.escape(o['texto'])}</code>\n")
    linhas.append("Botão: abre o X com a resposta escrita. Se cair no navegador, toque no texto para "
                  f"copiar e abra o <a href=\"{post}\">post no X</a>.")
    teclado = [[{"text": f"Abrir no X: {i}", "url": (
        f"https://x.com/intent/post?in_reply_to={tweet_id}&text={quote(o['texto'])}")}]
        for i, o in enumerate(opcoes, 1)]
    teclado.append([{"text": "🔄 Outras opções", "callback_data": f"outras:{tweet_id}:{usuario}"}])
    tg("sendMessage", chat_id=CHAT_ID, text="\n".join(linhas), parse_mode="HTML",
       reply_markup={"inline_keyboard": teclado}, disable_web_page_preview=True)


def perfis() -> list[str]:
    linhas = PERFIS.read_text(encoding="utf-8").splitlines() if PERFIS.exists() else []
    return [l.split("#")[0].strip().lstrip("@") for l in linhas if l.split("#")[0].strip()]


def descobrir(db):
    """Posts recentes com mais engajamento dos perfis da lista, ainda sem sugestão."""
    usuarios = perfis()
    if not usuarios:
        return
    # A busca aceita até 512 caracteres por consulta: divide a lista em blocos.
    blocos, atual = [], []
    for u in usuarios:
        if len(" OR ".join(f"from:{x}" for x in atual + [u])) > 440:
            blocos.append(atual)
            atual = []
        atual.append(u)
    blocos.append(atual)

    x = cliente_x()
    inicio = datetime.now(timezone.utc) - JANELA
    candidatos = []
    for bloco in blocos:
        consulta = f"({' OR '.join(f'from:{u}' for u in bloco)}) -is:retweet -is:reply"
        try:
            resp = x.search_recent_tweets(
                consulta, max_results=10, start_time=inicio, user_auth=True, sort_order="relevancy",
                tweet_fields=["public_metrics", "note_tweet", "author_id"],
                expansions=["author_id"], user_fields=["username"],
            )
        except tweepy.TweepyException as e:
            print(f"[respostas] busca falhou: {e}")
            continue
        nomes = {u.id: u.username for u in (resp.includes or {}).get("users", [])}
        for post in resp.data or []:
            m = post.public_metrics
            candidatos.append({
                "id": str(post.id), "usuario": nomes.get(post.author_id, "?"),
                "texto": (post.data.get("note_tweet") or {}).get("text") or post.text,
                "pontos": m["like_count"] + 2 * m["retweet_count"] + 3 * m["reply_count"] + 2 * m["quote_count"],
            })

    ja = {l["chave"].split(":", 1)[1] for l in
          db.table("bot_estado").select("chave").like("chave", "sugerido:%").execute().data}
    escolhidos = []
    for c in sorted(candidatos, key=lambda c: -c["pontos"]):
        # Um post por perfil por rodada, para variar.
        if c["id"] in ja or any(e["usuario"] == c["usuario"] for e in escolhidos):
            continue
        escolhidos.append(c)
        if len(escolhidos) == SUGESTOES_POR_RODADA:
            break
    print(f"[respostas] {len(candidatos)} posts lidos, {len(escolhidos)} escolhidos")
    for c in escolhidos:
        db.table("bot_estado").upsert({"chave": f"sugerido:{c['id']}", "valor": c["usuario"]}).execute()
        try:
            sugerir(c["id"], c["usuario"], c["texto"], contexto=f" ({c['pontos']} interações)")
        except Exception as e:
            print(f"[respostas] falha em {c['id']}: {e}")


def main():
    db = supabase()
    if "--descobrir" in sys.argv:
        descobrir(db)
        return
    pendentes = pedidos(db)
    if "--checar" in sys.argv:
        saida = os.getenv("GITHUB_OUTPUT")
        if saida:
            with open(saida, "a") as f:
                f.write(f"tem={'1' if pendentes else '0'}\n")
        print(f"[respostas] {len(pendentes)} pedidos")
        return
    for tweet_id in pendentes:
        valor = db.table("bot_estado").select("valor").eq("chave", f"pedido_resposta:{tweet_id}").execute().data
        db.table("bot_estado").delete().eq("chave", f"pedido_resposta:{tweet_id}").execute()
        try:
            responder(db, tweet_id, valor[0]["valor"] if valor else "")
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao gerar respostas para {tweet_id}: {e}")


if __name__ == "__main__":
    main()
