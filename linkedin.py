"""6) Posts para o LinkedIn pessoal, entregues prontos no Telegram para você copiar e colar.

O LinkedIn não deixa publicar em perfil pessoal sem app aprovado, então aqui não há publicação
automática: o post chega num bloco que copia com um toque, mais um botão que abre o LinkedIn.

Dois caminhos:
- `--diario` (no workflow de coleta, a cada 3 horas): uma vez por dia, a partir das 7h de Brasília,
  o Claude escolhe a notícia das últimas 24 horas que rende mais no LinkedIn e escreve o post;
- `/linkedin` no Telegram (chave pedido_linkedin:<msgid>): sem texto faz o mesmo que o diário,
  com texto usa a sua mensagem como pauta. respostas.py atende o pedido na rodada seguinte.
"""
import html
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from config import supabase
from gerador import claude_json
from publicador import BRASILIA
from telegram_bot import CHAT_ID, tg

VOZ = Path(__file__).with_name("voz_linkedin.md")
LIMITE = 3000  # limite do LinkedIn
HORA_DIARIO = 7  # Brasília

SCHEMA = {
    "type": "object",
    "properties": {
        "escrever": {"type": "boolean", "description": "false se não há fatos suficientes sem inventar"},
        "motivo": {"type": "string"},
        "fonte": {"type": "integer", "description": "índice da notícia escolhida; -1 num pedido"},
        "post": {"type": "string"},
    },
    "required": ["escrever", "motivo", "fonte", "post"],
    "additionalProperties": False,
}


def sistema() -> str:
    return (
        "You write posts for the personal LinkedIn profile of the Head of LATAM at BeInCrypto. "
        "The voice guide below defines tone, structure and hard limits; follow it strictly.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        "Rules for every post:\n"
        f"- Brazilian Portuguese only, at most {LIMITE - 300} characters.\n"
        "- Plain text: LinkedIn does not render markdown, so no asterisks, no headings, no bullet symbols "
        "other than an emoji or a number at the start of a line.\n"
        "- No links or URLs.\n"
        "- Sources come in Portuguese, Spanish or English. Write in your own words.\n"
        "- Only state facts present in the sources. Never invent numbers, quotes or predictions. "
        "The opinion and the reading of the facts are yours; the facts are not."
    )


def candidatas(db) -> list[dict]:
    desde = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return (db.table("fontes").select("titulo,resumo,url,origem,idioma").eq("tipo", "noticia")
            .gte("publicado_em", desde).order("publicado_em", desc=True).limit(30).execute().data)


def recentes(db) -> list[str]:
    linhas = db.table("bot_estado").select("valor").like("chave", "linkedin_post:%").execute().data
    return [l["valor"] for l in linhas][-10:]


def escrever_do_dia(db) -> dict | None:
    """O Claude escolhe a notícia das últimas 24 horas que rende o melhor post e escreve."""
    fontes = candidatas(db)
    if not fontes:
        return None
    lista = "\n\n".join(f"{i}. [{f['origem']}] {f['titulo']}\n{(f['resumo'] or '')[:500]}"
                        for i, f in enumerate(fontes))
    feitos = "\n".join(f"- {t}" for t in recentes(db)) or "- (none)"
    saida = claude_json(
        f"News from the last 24 hours:\n\n{lista}\n\n"
        f"Topics already posted on LinkedIn recently (do not repeat them):\n{feitos}\n\n"
        "Pick the one story that gives the best LinkedIn post for this audience: business, regulation, "
        "adoption, Latin America, media and marketing beat daily price moves. Set fonte to its index and "
        "write the post.",
        sistema(), SCHEMA,
    )
    if saida and saida["escrever"] and 0 <= saida["fonte"] < len(fontes):
        saida["origem"] = fontes[saida["fonte"]]["url"]
        return saida
    return None


def escrever_pedido(instrucao: str) -> dict | None:
    saida = claude_json(
        "The account owner asked for a LinkedIn post. Treat his message as the brief and as the only "
        "source of facts: do not add numbers, names or events he did not mention. escrever=false only if "
        "it is impossible without inventing facts; explain why in motivo.\n\n"
        f"His request:\n\"\"\"\n{instrucao}\n\"\"\"\n\nWrite the post.",
        sistema(), SCHEMA,
    )
    if saida and saida["escrever"]:
        saida["origem"] = "pedido seu no Telegram"
        return saida
    return None


def enviar(db, saida: dict):
    post = saida["post"].strip()
    if len(post) > LIMITE:
        tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ O post do LinkedIn saiu com {len(post)} caracteres, acima do limite.")
        return
    texto = (f"💼 <b>Post para o LinkedIn</b> ({len(post)} caracteres)\n"
             f"Fonte, só para você conferir: {html.escape(saida['origem'])}\n\n"
             f"<pre>{html.escape(post)}</pre>\n\n"
             "Toque no texto (ou em Copiar) e cole no LinkedIn.")
    abrir = {"inline_keyboard": [[{"text": "Abrir no LinkedIn",
                                   "url": f"https://www.linkedin.com/feed/?shareActive=true&text={quote(post)}"}]]}
    vazio = {"inline_keyboard": [[{"text": "Abrir no LinkedIn", "url": "https://www.linkedin.com/feed/?shareActive=true"}]]}
    # Botão com o texto já escrito; se a URL ficar longa demais para o Telegram, abre o LinkedIn vazio.
    if not tg("sendMessage", silencioso=True, chat_id=CHAT_ID, text=texto, parse_mode="HTML",
              reply_markup=abrir, disable_web_page_preview=True):
        tg("sendMessage", chat_id=CHAT_ID, text=texto, parse_mode="HTML",
           reply_markup=vazio, disable_web_page_preview=True)
    agora = datetime.now(timezone.utc).isoformat()
    db.table("bot_estado").upsert({"chave": f"linkedin_post:{agora}", "valor": post.splitlines()[0][:140]}).execute()


def diario(db):
    hoje = datetime.now(BRASILIA)
    feito = db.table("bot_estado").select("valor").eq("chave", "linkedin:dia").execute().data
    if hoje.hour < HORA_DIARIO or (feito and feito[0]["valor"] == hoje.date().isoformat()):
        print("[linkedin] post do dia já enviado ou ainda cedo")
        return
    saida = escrever_do_dia(db)
    if not saida:
        print("[linkedin] nenhuma notícia boa hoje")
        return
    enviar(db, saida)
    db.table("bot_estado").upsert({"chave": "linkedin:dia", "valor": hoje.date().isoformat()}).execute()


def atender_pedidos(db):
    for p in db.table("bot_estado").select("*").like("chave", "pedido_linkedin:%").execute().data:
        db.table("bot_estado").delete().eq("chave", p["chave"]).execute()
        try:
            saida = escrever_pedido(p["valor"]) if p["valor"].strip() else escrever_do_dia(db)
            if saida:
                enviar(db, saida)
            else:
                tg("sendMessage", chat_id=CHAT_ID, text="⚠️ Não consegui escrever o post do LinkedIn sem "
                   "inventar fatos. Mande mais detalhes (números, nomes, o que aconteceu).")
        except Exception as e:
            tg("sendMessage", chat_id=CHAT_ID, text=f"⚠️ Falha ao gerar o post do LinkedIn: {e}")


if __name__ == "__main__":
    banco = supabase()
    diario(banco) if "--diario" in sys.argv else atender_pedidos(banco)
