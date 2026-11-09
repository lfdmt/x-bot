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
import json
import os
import random
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


def pedidos_de_tweet(db) -> list[dict]:
    return db.table("bot_estado").select("*").like("chave", "pedido_tweet:%").execute().data


PALAVRAS_VAZIAS = {"the", "a", "an", "of", "in", "on", "to", "for", "and", "as", "from", "with", "by", "at",
                   "o", "a", "os", "as", "de", "do", "da", "dos", "das", "em", "no", "na", "e", "para", "com",
                   "after", "over", "since", "amid", "its", "is", "are", "across"}


def posts_em_portugues(consulta: str) -> list[str]:
    """Até 10 posts recentes e relevantes em português sobre o assunto: a conversa no X Brasil."""
    try:
        resp = cliente_x().search_recent_tweets(
            f"{consulta} lang:pt -is:retweet -is:reply", max_results=10, sort_order="relevancy",
            user_auth=True, tweet_fields=["note_tweet"])
    except tweepy.TweepyException as e:
        print(f"[respostas] busca em português falhou: {e}")
        return []
    return [(p.data.get("note_tweet") or {}).get("text") or p.text for p in resp.data or []]


def montar_atualidades(db):
    from pauta import assuntos_agrupados

    if not db.table("bot_estado").select("chave").eq("chave", "pedido_atualidades").execute().data:
        return
    db.table("bot_estado").delete().eq("chave", "pedido_atualidades").execute()
    try:
        assuntos = assuntos_agrupados()
    except Exception as e:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Não consegui montar os assuntos do momento: {e}")
        return
    if not assuntos:
        tg("sendMessage", chat_id=CHAT_ID, text="Não achei assuntos fortes nas últimas horas.")
        return
    db.table("bot_estado").upsert({"chave": "atualidades", "valor": json.dumps(assuntos, ensure_ascii=False)}).execute()
    linhas = ["📰 Assuntos do momento no Brasil:\n"]
    for i, a in enumerate(assuntos):
        linhas.append(f"{i + 1}. {a['name']} ({a['portais']} portais)\n   {a['summary']}")
    linhas.append("\nToque no número para eu escrever um rascunho. Leio também o que estão postando no X sobre o tema.")
    botoes = [{"text": str(i + 1), "callback_data": f"alta:{i}"} for i in range(len(assuntos))]
    tg("sendMessage", chat_id=CHAT_ID, text="\n".join(linhas),
       reply_markup={"inline_keyboard": [botoes[i:i + 5] for i in range(0, len(botoes), 5)]})


def posts_sobre(assunto: str) -> list[str]:
    """Até 10 posts recentes e relevantes sobre o assunto, como fonte de fatos."""
    # Sem "$": na busca do X, "$12.8" vira filtro de cashtag e traz posts de outro assunto.
    termos = [t.strip(".") for t in re.findall(r"[\w.]+", assunto.replace("$", " "))
              if t.lower() not in PALAVRAS_VAZIAS and len(t.strip(".")) > 1]
    x = cliente_x()
    for qtd in (5, 3, 2):  # se a busca vier vazia, afrouxa
        consulta = " ".join(termos[:qtd]) + " -is:retweet -is:reply"
        try:
            resp = x.search_recent_tweets(consulta, max_results=10, sort_order="relevancy", user_auth=True,
                                          tweet_fields=["note_tweet"])
        except tweepy.TweepyException as e:
            print(f"[respostas] busca do assunto falhou: {e}")
            return []
        if resp.data:
            return [(p.data.get("note_tweet") or {}).get("text") or p.text for p in resp.data]
    return []


def gerar_e_avisar(db, gerar_pedido, enviar_pendentes, instrucao: str, assunto: str,
                   origem: str = "em alta no X") -> bool:
    try:
        if gerar_pedido(db, instrucao, origem=f"{origem}: {assunto}"):
            tg("sendMessage", chat_id=CHAT_ID, text=f"✅ Pronto o rascunho sobre \"{assunto}\" 👇")
            enviar_pendentes(db)
            return True
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Não deu para escrever sobre \"{assunto}\" sem inventar fatos.")
    except Exception as e:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao escrever sobre \"{assunto}\": {e}")
    return False


def atender_assuntos(db):
    from gerador import gerar_pedido
    from telegram_bot import enviar_pendentes

    for p in db.table("bot_estado").select("*").like("chave", "pedido_alta:%").execute().data:
        db.table("bot_estado").delete().eq("chave", p["chave"]).execute()
        try:
            noticia = json.loads(p["valor"])
        except ValueError:
            noticia = {"name": p["valor"]}
        assunto = noticia["name"]
        if noticia.get("fontes"):
            # Assunto do Brasil: manchetes dos portais são os fatos; os posts no X dão o tom da conversa.
            posts = posts_em_portugues(noticia.get("busca_x") or assunto)
            conversa = "\n---\n".join(t[:400] for t in posts) or "(nenhum post encontrado)"
            instrucao = (
                f"Tweet sobre este assunto do momento no Brasil: \"{assunto}\".\n"
                "Facts come ONLY from these news headlines and summaries:\n"
                f"{noticia['fontes']}\n\n"
                "What people are posting about it on X right now (for tone and angle, not as a source of "
                f"facts):\n{conversa}")
            gerar_e_avisar(db, gerar_pedido, enviar_pendentes, instrucao, assunto, origem="assunto do momento")
            continue
        if noticia.get("summary"):
            # Notícia do X: o resumo já é a fonte, sem gastar leitura de posts.
            instrucao = (
                f"Tweet sobre esta notícia do momento no X: \"{assunto}\".\n"
                "Use as the only source of facts the headline and summary below; keep the same figures, "
                "period and actors.\n"
                f"Summary: {noticia['summary']}\nHook: {noticia.get('hook') or ''}")
            gerar_e_avisar(db, gerar_pedido, enviar_pendentes, instrucao, assunto)
            continue
        posts = posts_sobre(assunto)
        if not posts:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Não achei posts sobre \"{assunto}\" para usar como base.")
            continue
        contexto = "\n---\n".join(t[:600] for t in posts)
        instrucao = (
            f"Tweet sobre ESTE assunto em alta no X agora: \"{assunto}\".\n"
            "The headline above is the story and its numbers are the main facts: the post must be about exactly "
            "this angle (same figure, same period, same actors). The posts below are only supporting context; "
            "use a detail from them only if it is about this same story and consistent with the headline. "
            "If they cover a different figure or angle, ignore them.\n"
            f"Supporting posts:\n{contexto}")
        try:
            if gerar_pedido(db, instrucao, origem=f"em alta no X: {assunto}"):
                tg("sendMessage", chat_id=CHAT_ID, text=f"✅ Pronto o rascunho sobre \"{assunto}\" 👇")
                enviar_pendentes(db)
            else:
                tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Os posts sobre \"{assunto}\" não trouxeram fatos "
                   "suficientes para um tweet sem inventar nada.")
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao escrever sobre \"{assunto}\": {e}")


def atender_pedidos_de_tweet(db):
    from gerador import gerar_pedido
    from telegram_bot import enviar_pendentes

    for p in pedidos_de_tweet(db):
        db.table("bot_estado").delete().eq("chave", p["chave"]).execute()
        try:
            if not gerar_pedido(db, p["valor"]):
                tg("sendMessage", chat_id=CHAT_ID, text="⚠️ Não consegui escrever esse sem inventar fatos. "
                   "Mande mais detalhes (números, nomes, o que aconteceu).")
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao gerar o tweet pedido: {e}")
    enviar_pendentes(db)


ANGULOS_NICHO = (
    "- Write exactly 3 replies, each with a different angle: an insight or LATAM perspective, a "
    "respectful counterpoint, and a sharp question that invites the author to answer.\n")
# Virais e atualidades (política, dia a dia, corporativo, startups, geopolítica): o objetivo é a resposta
# que ganha curtida e vira conversa, muitas vezes pelo humor.
ANGULOS_VIRAL = (
    "- This is a viral or trending post (politics, everyday life, corporate world, startups or world "
    "news). The goal is a reply people like, quote and screenshot, the kind that ends up as a top reply. "
    "Write exactly 3 replies, each with a different angle:\n"
    "  1. Humor: a witty, ironic or meme-style line, very Brazilian Twitter (dry irony, absurd comparison, "
    "relatable everyday situation, 'quem nunca'). It must be clever, not silly.\n"
    "  2. Viral: a bold, punchy, relatable take that many people agree with and want to share.\n"
    "  3. Visão própria: a sharp take from someone who lives business, tech and LATAM, or a question "
    "that makes people answer. Brazil angle when it fits.\n"
    "- Never joke about deaths, victims, war crimes, disasters, illness or human suffering. If the post "
    "is about that, replace the humor angle with a sober, human line.\n"
    "- Politics: aim irony at the situation and at the powerful, never at voters, a people or a religion. "
    "No insults, no slurs, no partisan flame war, no defending a party or politician.\n")


def sistema(ingles: bool = False, viral: bool = False) -> str:
    estilo = "casual Twitter English" if viral else "casual crypto Twitter English"
    idioma = (f"- Always in English, {estilo}.\n" if ingles else
              "- Always in Brazilian Portuguese, whatever the language of the original post.\n")
    abreviacoes = ("u, ur, rn, imo, tbh, ngl, gonna, kinda, lol" if ingles else
                   "vc, mto, tb, pq, q, pra, tá, msm, td, hj, blz")
    return (
        "You write replies for the personal X account of the Head of LATAM at BeInCrypto. "
        "The voice guide below defines his tone; follow it.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        "Rules for every reply:\n"
        f"{ANGULOS_VIRAL if viral else ANGULOS_NICHO}"
        f"{idioma}"
        f"- At most {LIMITE_TWEET - 20} characters each. No links, no hashtags, at most one emoji.\n"
        "- Use only facts stated in the post. Never bring outside numbers, names or events: offer "
        "opinion and angle instead.\n"
        "- Add something to the conversation. No empty praise ('great post'), no self promotion.\n"
        "- angulo is always a 1 to 3 word label in Portuguese (e.g. "
        + ("'Humor', 'Viral', 'Visão própria'" if viral else "'Visão LATAM', 'Contraponto', 'Pergunta'") + ").\n"
        "- Sound like a real person typing fast on the phone, never like an AI. Standard capitalization (sentences start with a capital letter). Use internet abbreviations where natural: "
        f"{abreviacoes}. Light punctuation: no final period, no em dash, "
        "no semicolon, no colon. Short, usually under 160 characters, one or two sentences.\n"
        "- Avoid AI tells: 'não é X, é Y', 'isso mostra que', 'vale lembrar', 'em suma', 'o ponto é', "
        "lists of three, polished parallel sentences, starting with 'Excelente' or 'Interessante'."
    )


def casual(texto: str) -> str:
    """Garante o tom digitado no celular: sem travessão nem ponto final. A caixa das letras fica como veio."""
    return texto.replace(" — ", ", ").replace(" – ", ", ").replace("—", ",").strip().rstrip(".")


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
    conta = lista_de_perfis().get(usuario.lower(), {})
    saida = claude_json(f"Post by @{usuario}:\n\"\"\"\n{texto}\n\"\"\"\n\nWrite the 3 replies.",
                        sistema(conta.get("ingles", False), conta.get("viral", False)), SCHEMA)
    opcoes = [{**o, "texto": casual(o["texto"])} for o in (saida or {}).get("respostas", [])]
    opcoes = [o for o in opcoes if len(o["texto"]) <= LIMITE_TWEET][:3]
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


MIN_SEGUIDORES = int(os.getenv("MIN_SEGUIDORES", "5000"))
# Uma conta que apareceu numa lista fica de fora das próximas por este tempo.
DESCANSO = timedelta(hours=int(os.getenv("DESCANSO_HORAS", "6")))
POSTS_EM_ALTA = int(os.getenv("POSTS_EM_ALTA", "5"))
JANELA_ALTA = timedelta(hours=6)


# Contas destes temas vão para a lista de virais e atualidades (o comentário da linha começa pelo tema).
TEMAS_VIRAIS = ("geopolítica", "política", "dia a dia", "corporativo", "startups")


def lista_de_perfis() -> dict[str, dict]:
    """@ em minúsculas -> usuario, seguidores, viral e ingles, tirados do comentário da linha."""
    linhas = PERFIS.read_text(encoding="utf-8").splitlines() if PERFIS.exists() else []
    saida = {}
    for l in linhas:
        usuario, _, comentario = l.partition("#")
        usuario = usuario.strip().lstrip("@")
        if usuario:
            seguidores = re.search(r"([\d.]+) seguidores", comentario)
            saida[usuario.lower()] = {
                "usuario": usuario,
                "seguidores": int(seguidores.group(1).replace(".", "")) if seguidores else 0,
                "viral": comentario.strip().lower().startswith(TEMAS_VIRAIS),
                "tema": re.split(r"[.,]", comentario.strip().lower(), maxsplit=1)[0].strip(),
                "ingles": "em inglês" in comentario.lower(),
            }
    return saida


def perfis(minimo: int = 0, viral: bool = False) -> list[str]:
    return [c["usuario"] for c in lista_de_perfis().values() if c["seguidores"] >= minimo and c["viral"] == viral]


def sortear_bloco(db, usuarios: list[str]) -> list[str]:
    """Um grupo de contas que cabe numa busca (512 caracteres), sorteado entre as que não apareceram há pouco."""
    agora = datetime.now(timezone.utc)
    vistos = {l["chave"].split(":", 1)[1]: datetime.fromisoformat(l["valor"]) for l in
              db.table("bot_estado").select("*").like("chave", "mostrado:%").execute().data}
    descansadas = [u for u in usuarios if agora - vistos.get(u, datetime.min.replace(tzinfo=timezone.utc)) > DESCANSO]
    random.shuffle(descansadas)
    resto = [u for u in usuarios if u not in descansadas]
    random.shuffle(resto)
    bloco = []
    for u in descansadas + resto:
        if len(" OR ".join(f"from:{x}" for x in bloco + [u])) <= 440:
            bloco.append(u)
    return bloco


def descobrir(db) -> bool:
    """Uma lista de posts em alta para cada nicho: cripto e mercado, e virais e atualidades."""
    achou = False
    for viral in (False, True):
        try:
            achou = descobrir_nicho(db, viral) or achou
        except Exception as e:
            print(f"[respostas] em alta ({'virais' if viral else 'nicho'}) falhou: {e}")
    return achou


def descobrir_nicho(db, viral: bool) -> bool:
    """Posts em alta das contas grandes e médias da lista, para você escolher quais responder.

    Cada rodada lê 10 posts (uns US$ 0,05) de um grupo sorteado de contas, dando preferência às que
    não apareceram nas últimas horas. O ranking é por engajamento em relação ao tamanho da conta, para
    as médias competirem com as grandes, e entra no máximo um post por conta. As sugestões de resposta
    só são escritas para o post que você escolher no botão.
    """
    contas = lista_de_perfis()
    bloco = sortear_bloco(db, perfis(MIN_SEGUIDORES, viral))
    if not bloco:
        return False
    consulta = f"({' OR '.join(f'from:{u}' for u in bloco)}) -is:retweet -is:reply"
    agora = datetime.now(timezone.utc)
    try:
        resp = cliente_x().search_recent_tweets(
            consulta, max_results=10, start_time=agora - JANELA_ALTA, user_auth=True, sort_order="relevancy",
            tweet_fields=["public_metrics", "note_tweet", "author_id", "created_at"],
            expansions=["author_id"], user_fields=["username"],
        )
    except tweepy.TweepyException as e:
        print(f"[respostas] busca falhou: {e}")
        return False
    nomes = {u.id: u.username for u in (resp.includes or {}).get("users", [])}
    ja = {l["chave"].split(":", 1)[1] for l in
          db.table("bot_estado").select("chave").like("chave", "sugerido:%").execute().data}
    candidatos = []
    for post in resp.data or []:
        if str(post.id) in ja:
            continue
        m = post.public_metrics
        candidatos.append({
            "id": str(post.id), "usuario": nomes.get(post.author_id, "?"),
            "texto": (post.data.get("note_tweet") or {}).get("text") or post.text,
            "horas": max(0, int((agora - post.created_at).total_seconds() // 3600)),
            "pontos": m["like_count"] + 2 * m["retweet_count"] + 3 * m["reply_count"] + 2 * m["quote_count"],
        })
    # Engajamento por mil seguidores: uma conta média com post bombando passa na frente de uma grande parada.
    taxa = lambda c: c["pontos"] / max(contas.get(c["usuario"].lower(), {}).get("seguidores", 0), 1000) * 1000
    escolhidos = []
    for c in sorted(candidatos, key=lambda c: -taxa(c)):
        if all(e["usuario"].lower() != c["usuario"].lower() for e in escolhidos):
            escolhidos.append(c)
        if len(escolhidos) == POSTS_EM_ALTA:
            break
    print(f"[respostas] {'virais' if viral else 'nicho'}: {len(bloco)} contas, {len(resp.data or [])} posts lidos, {len(escolhidos)} escolhidos")
    if not escolhidos:
        return False

    titulo = "🌎 <b>Em alta: virais e atualidades</b>" if viral else "🔥 <b>Em alta nas contas do seu nicho</b>"
    linhas = [f"{titulo}. Toque em Responder para receber 3 sugestões.\n"]
    teclado = []
    for n, c in enumerate(escolhidos, 1):
        db.table("bot_estado").upsert({"chave": f"sugerido:{c['id']}", "valor": c["usuario"]}).execute()
        db.table("bot_estado").upsert({"chave": f"mostrado:{c['usuario']}", "valor": agora.isoformat()}).execute()
        resumo = c["texto"] if len(c["texto"]) <= 220 else c["texto"][:220] + "…"
        quando = "agora há pouco" if c["horas"] == 0 else f"há {c['horas']} h"
        linhas.append(f"{n}. <a href=\"https://x.com/{c['usuario']}/status/{c['id']}\">@{html.escape(c['usuario'])}</a>"
                      f" · {c['pontos']} interações · {quando}\n<i>{html.escape(resumo)}</i>\n")
        teclado.append({"text": f"💬 Responder {n}", "callback_data": f"outras:{c['id']}:{c['usuario']}"[:64]})
    tg("sendMessage", chat_id=CHAT_ID, text="\n".join(linhas), parse_mode="HTML", disable_web_page_preview=True,
       reply_markup={"inline_keyboard": [teclado[k:k + 3] for k in range(0, len(teclado), 3)]})
    return True


def atender_virais(db):
    from gerador import gerar_virais
    from telegram_bot import enviar_pendentes

    for p in db.table("bot_estado").select("*").like("chave", "pedido_viral:%").execute().data:
        db.table("bot_estado").delete().eq("chave", p["chave"]).execute()
        try:
            from pauta import manchetes_do_dia
            ids = gerar_virais(db, manchetes_do_dia(), p["valor"].strip())
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao escrever os tweets virais: {e}")
            continue
        if ids:
            tg("sendMessage", chat_id=CHAT_ID, text=f"✅ {len(ids)} tweets do dia a dia 👇")
            enviar_pendentes(db)
        else:
            tg("sendMessage", chat_id=CHAT_ID, text="⚠️ Não saiu nenhum tweet bom. Mande /viral de novo.")


def main():
    db = supabase()
    if "--descobrir" in sys.argv:
        descobrir(db)
        return
    pendentes = pedidos(db)
    if "--checar" in sys.argv:
        tem = bool(pendentes or pedidos_de_tweet(db) or
                   db.table("bot_estado").select("chave").like("chave", "pedido_a%").execute().data or
                   db.table("bot_estado").select("chave").like("chave", "pedido_viral:%").execute().data or
                   db.table("bot_estado").select("chave").like("chave", "pedido_linkedin:%").execute().data)
        saida = os.getenv("GITHUB_OUTPUT")
        if saida:
            with open(saida, "a") as f:
                f.write(f"tem={'1' if tem else '0'}\n")
        print(f"[respostas] há pedidos: {tem}")
        return
    montar_atualidades(db)
    atender_assuntos(db)
    atender_pedidos_de_tweet(db)
    atender_virais(db)
    from linkedin import atender_pedidos as atender_linkedin
    atender_linkedin(db)
    for tweet_id in pendentes:
        valor = db.table("bot_estado").select("valor").eq("chave", f"pedido_resposta:{tweet_id}").execute().data
        db.table("bot_estado").delete().eq("chave", f"pedido_resposta:{tweet_id}").execute()
        try:
            responder(db, tweet_id, valor[0]["valor"] if valor else "")
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao gerar respostas para {tweet_id}: {e}")


if __name__ == "__main__":
    main()
