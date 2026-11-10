"""/seguir no Telegram: 5 contas para você seguir, com chance de seguirem de volta. Grátis, sem API do X.

O bot não segue ninguém (seguir em massa por automação é contra as regras do X): ele só sugere e
abre o perfil, e você toca em Seguir. Vêm primeiro os autores dos posts que você respondeu pelo bot
(já viram seu nome), depois a lista do seguir.txt. Nenhuma conta é sugerida duas vezes.
"""
import random
from datetime import datetime, timezone
from pathlib import Path

LISTA = Path(__file__).with_name("seguir.txt")
POR_VEZ = 5


def lista_inicial() -> list[tuple[str, str]]:
    contas = []
    for linha in LISTA.read_text(encoding="utf-8").splitlines():
        usuario, _, comentario = linha.partition("#")
        if usuario.strip():
            contas.append((usuario.strip().lstrip("@"), comentario.strip()))
    return contas


def lembrar_autor(db, usuario: str):
    """Guarda o autor de um post que você respondeu: bom candidato a seguir de volta."""
    if usuario and usuario != "?":
        db.table("bot_estado").upsert({"chave": f"seguir_autor:{usuario.lower()}",
                                       "valor": usuario}).execute()


def sugerir(db) -> tuple[str, dict | None]:
    estado = db.table("bot_estado").select("chave,valor").like("chave", "seguir_%").execute().data
    ja = {l["chave"].split(":", 1)[1] for l in estado if l["chave"].startswith("seguir_sugerido:")}
    autores = [(l["valor"], "você respondeu um post") for l in estado if l["chave"].startswith("seguir_autor:")]
    lista = lista_inicial()
    random.shuffle(lista)  # mistura os temas a cada pedido
    escolhidas = []
    for usuario, nota in autores + lista:
        if usuario.lower() not in ja and all(u.lower() != usuario.lower() for u, _ in escolhidas):
            escolhidas.append((usuario, nota))
        if len(escolhidas) == POR_VEZ:
            break
    if not escolhidas:
        return ("Acabaram as sugestões. Acrescente contas no seguir.txt ou responda mais posts pelo bot, "
                "que os autores entram na lista."), None
    agora = datetime.now(timezone.utc).isoformat()
    for usuario, _ in escolhidas:
        db.table("bot_estado").upsert({"chave": f"seguir_sugerido:{usuario.lower()}", "valor": agora}).execute()
    linhas = ["👥 Contas para seguir. Responder um post antes de seguir aumenta a chance de seguirem de volta.\n"]
    linhas += [f"• @{u}" + (f": {n}" if n else "") for u, n in escolhidas]
    teclado = [[{"text": f"Abrir @{u}", "url": f"https://x.com/{u}"}] for u, _ in escolhidas]
    return "\n".join(linhas), {"inline_keyboard": teclado}
