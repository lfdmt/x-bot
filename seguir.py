"""/seguir no Telegram: 5 contas para você seguir, com chance de seguirem de volta. Grátis, sem API do X.

O bot não segue ninguém (seguir em massa por automação é contra as regras do X): ele só sugere e
abre o perfil, e você toca em Seguir. Vêm primeiro os autores dos posts que você respondeu pelo bot
(já viram seu nome, e são contas reais que postaram há pouco), depois o que você puser no
seguir.txt. Nunca adivinhar @: só entram contas que o bot viu postando ou que você conferiu.
Nenhuma conta é sugerida duas vezes.
"""
import random
from datetime import datetime, timezone
from pathlib import Path

LISTA = Path(__file__).with_name("seguir.txt")
JA_SIGO = Path(__file__).with_name("perfis_resposta.txt")  # contas que você já segue
POR_VEZ = 5


def lista_inicial() -> list[tuple[str, str]]:
    contas = []
    for linha in LISTA.read_text(encoding="utf-8").splitlines():
        usuario, _, comentario = linha.partition("#")
        if usuario.strip():
            contas.append((usuario.strip().lstrip("@"), comentario.strip()))
    return contas


def ja_sigo() -> set[str]:
    return {l.split("#")[0].strip().lower() for l in JA_SIGO.read_text(encoding="utf-8").splitlines()
            if l.split("#")[0].strip()}


def lembrar_autor(db, usuario: str):
    """Guarda o autor de um post que você respondeu: bom candidato a seguir de volta."""
    if usuario and usuario != "?":
        db.table("bot_estado").upsert({"chave": f"seguir_autor:{usuario.lower()}",
                                       "valor": usuario}).execute()


def sugerir(db) -> tuple[str, dict | None]:
    estado = db.table("bot_estado").select("chave,valor").like("chave", "seguir_%").execute().data
    # Fora: já sugeridas, as que você tirou pelo botão e as que você já segue.
    ja = {l["chave"].split(":", 1)[1] for l in estado
          if l["chave"].startswith(("seguir_sugerido:", "seguir_fora:"))} | ja_sigo()
    autores = [(l["valor"].partition("|")[0], l["valor"].partition("|")[2] or "você respondeu um post")
               for l in estado if l["chave"].startswith("seguir_autor:")]
    lista = lista_inicial()
    random.shuffle(lista)  # mistura os temas a cada pedido
    escolhidas = []
    for usuario, nota in autores + lista:
        if usuario.lower() not in ja and all(u.lower() != usuario.lower() for u, _ in escolhidas):
            escolhidas.append((usuario, nota))
        if len(escolhidas) == POR_VEZ:
            break
    if not escolhidas:
        return ("Ainda não tenho contas novas para sugerir. Mande /seguir buscar: o bot acha quem anda "
                "respondendo as contas do seu nicho (contas reais e ativas). Usa a API do X, uns US$ 0,75 "
                "por busca."), None
    agora = datetime.now(timezone.utc).isoformat()
    for usuario, _ in escolhidas:
        db.table("bot_estado").upsert({"chave": f"seguir_sugerido:{usuario.lower()}", "valor": agora}).execute()
    linhas = ["👥 Contas para seguir. Responder um post antes de seguir aumenta a chance de seguirem de volta.\n"]
    linhas += [f"• @{u}" + (f": {n}" if n else "") for u, n in escolhidas]
    teclado = [[{"text": f"Abrir @{u}", "url": f"https://x.com/{u}"},
                {"text": "🚫 Tirar", "callback_data": f"seguirfora:{u}"[:64]}] for u, _ in escolhidas]
    linhas.append("\n🚫 Tirar: a conta não existe, está parada ou não tem a ver. Ela nunca mais aparece.")
    return "\n".join(linhas), {"inline_keyboard": teclado}


def tirar(db, usuario: str):
    db.table("bot_estado").upsert({"chave": f"seguir_fora:{usuario.lower()}", "valor": usuario}).execute()
    db.table("bot_estado").delete().eq("chave", f"seguir_autor:{usuario.lower()}").execute()


# Coleta paga e pontual (/seguir buscar): quem respondeu, nos últimos dias, posts de contas do seu nicho.
# São contas reais, ativas e engajadas no seu assunto. Custo: até 50 posts (US$ 0,005) e os perfis dos
# autores (US$ 0,01 cada), uns US$ 0,75 por coleta, que rendem dezenas de sugestões.
SEGUIDORES_MIN, SEGUIDORES_MAX = 200, 30000


def buscar(db) -> int:
    """Enche a lista do /seguir. Devolve quantas contas novas entraram."""
    import tweepy

    from publicador import cliente_x
    from respostas import SemCredito, lista_de_perfis

    fontes = [c["usuario"] for c in lista_de_perfis().values()
              if not c.get("ingles") and 5000 <= c["seguidores"] <= 400000]
    random.shuffle(fontes)
    bloco = []
    for u in fontes:  # a consulta do X aceita até 512 caracteres
        if len(" OR ".join(f"to:{x}" for x in bloco + [u])) > 420:
            break
        bloco.append(u)
    try:
        resp = cliente_x().search_recent_tweets(
            f"({' OR '.join(f'to:{u}' for u in bloco)}) is:reply lang:pt -is:retweet", max_results=50,
            user_auth=True, expansions=["author_id"],
            user_fields=["username", "public_metrics", "description", "protected"])
    except tweepy.TweepyException as e:
        if "402" in str(e) or "credits" in str(e).lower():
            raise SemCredito() from e
        raise
    fora = ja_sigo() | {l["chave"].split(":", 1)[1] for l in
                        db.table("bot_estado").select("chave").like("chave", "seguir_%").execute().data}
    novas = 0
    for u in (resp.includes or {}).get("users", []):
        m = u.public_metrics or {}
        if (u.username.lower() in fora or u.protected
                or not SEGUIDORES_MIN <= m.get("followers_count", 0) <= SEGUIDORES_MAX):
            continue
        bio = " ".join((u.description or "").split())[:80]
        nota = f"{m['followers_count']:,} seguidores".replace(",", ".") + (f". {bio}" if bio else "")
        db.table("bot_estado").upsert({"chave": f"seguir_autor:{u.username.lower()}",
                                       "valor": f"{u.username}|{nota}"}).execute()
        fora.add(u.username.lower())
        novas += 1
    return novas
