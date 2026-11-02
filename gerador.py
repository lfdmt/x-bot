"""2) Gera rascunhos de tweet em português, com mídia, e salva como "pendente".

Usa o Claude Code em modo não interativo (`claude -p`) com a sua assinatura
Pro/Max, então não consome crédito de API. Localmente usa o login do `claude`;
no GitHub Actions usa o CLAUDE_CODE_OAUTH_TOKEN gerado com `claude setup-token`.
"""
import json
import os
import subprocess
from pathlib import Path

from config import supabase
from midia import buscar_gif, grafico, imagem_da_materia, logos_do_x, salvar_publico

MODELO = "claude-opus-5-5"
LIMITE_TWEET = 280
POR_EXECUCAO = int(os.getenv("RASCUNHOS_POR_EXECUCAO", "5"))
VOZ = Path(__file__).with_name("voz.md")
REFERENCIA = Path(__file__).with_name("referencia.md")

SCHEMA = {
    "type": "object",
    "properties": {
        "publicar": {
            "type": "boolean",
            "description": "false se a notícia não combina com os temas ou limites do voz.md",
        },
        "motivo": {"type": "string"},
        "tweet": {"type": "string"},
        "midia": {"type": "string", "enum": ["materia", "grafico", "gif", "logos", "nenhuma"]},
        "gif_busca": {"type": "string", "description": "termo em inglês para buscar o GIF; vazio se midia != gif"},
        "perfis": {
            "type": "array", "items": {"type": "string"},
            "description": "até 2 @ do X das empresas ou projetos centrais da notícia, para midia=logos",
        },
    },
    "required": ["publicar", "motivo", "tweet", "midia", "gif_busca", "perfis"],
    "additionalProperties": False,
}


def system_prompt() -> str:
    return (
        "You write posts for the personal X account of the Head of LATAM at BeInCrypto. "
        "The voice guide below defines tone, themes and hard limits; follow it strictly.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        "The reference below shows formats that work on @beincrypto, adapted to Portuguese. "
        "Use them as a repertoire; the voice guide still wins on tone.\n\n"
        f"<format_reference>\n{REFERENCIA.read_text(encoding='utf-8')}\n</format_reference>\n\n"
        "Rules for every post:\n"
        f"- Brazilian Portuguese only, at most {LIMITE_TWEET - 10} characters.\n"
        "- No links or URLs, and do not tell readers to click or read the article: the post stands alone.\n"
        "- Sources come in Portuguese, Spanish or English: news articles, market data, or posts from @beincrypto. "
        "Always write in Portuguese, in your own words; never translate a source post line by line.\n"
        "- Only state facts present in the source. Never invent numbers, quotes or predictions.\n"
        "- If the source falls outside the themes or touches a limit in the guide, set publicar=false.\n"
        "- Pick the media from the options offered with each source. The post must complement the media, "
        "never describe it. Prefer 'grafico' for market data; 'logos' when the story is about one or two "
        "well known companies or projects (give their exact X handles in perfis, and you may @mention them "
        "in the post, as @beincrypto does); 'materia' for other news; 'gif' only when the post is light or "
        "ironic, with a short English search term in gif_busca."
    )


def opcoes_de_midia(fonte: dict) -> list[str]:
    opcoes = ["nenhuma"]
    if fonte["tipo"] != "mercado":
        opcoes.append("materia")
        if os.getenv("X_ACCESS_TOKEN"):
            opcoes.append("logos")
    if fonte["tipo"] == "mercado" and (fonte.get("dados") or {}).get("serie"):
        opcoes.append("grafico")
    if os.getenv("GIPHY_API_KEY"):
        opcoes.append("gif")
    return opcoes


def resolver_midia(fonte: dict, saida: dict) -> tuple[str | None, list[str] | None]:
    """Transforma a escolha do Claude em (midia_tipo, midia_urls). Se falhar, tenta a imagem da matéria."""
    escolha = saida["midia"]
    try:
        if escolha == "grafico":
            png = grafico(fonte.get("dados") or {})
            if png:
                return "grafico", [salvar_publico(png, "png", "image/png")]
        if escolha == "logos":
            cards = logos_do_x(saida["perfis"])
            if cards:
                return "logos", [salvar_publico(c, "png", "image/png") for c in cards]
        if escolha == "gif":
            url = buscar_gif(saida["gif_busca"])
            if url:
                return "gif", [url]
    except Exception as e:  # mídia nunca deve impedir o rascunho
        print(f"[gerador] falha ao preparar {escolha}: {e}")
    if escolha != "nenhuma" and fonte["tipo"] != "mercado":
        url = fonte.get("imagem_url") or imagem_da_materia(fonte["url"])
        if url:
            return "materia", [url]
    return None, None


def gerar_um(fonte: dict) -> dict | None:
    pedido = (
        f"Source type: {fonte['tipo']} (from {fonte.get('origem') or 'unknown'})\n"
        f"Source language: {fonte['idioma']}\n"
        f"Title: {fonte['titulo']}\n"
        f"Content: {fonte['resumo']}\n"
        f"Media options: {', '.join(opcoes_de_midia(fonte))}\n\n"
        "Write one post about this."
    )
    # Sem ANTHROPIC_API_KEY no ambiente, o claude usa a assinatura e não a API paga.
    ambiente = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [
            "claude", "-p", pedido,
            "--model", MODELO,
            "--system-prompt", system_prompt(),
            "--json-schema", json.dumps(SCHEMA),
            "--output-format", "json",
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ],
        capture_output=True, text=True, timeout=300, env=ambiente,
    )
    saida = json.loads(proc.stdout or "{}")
    if proc.returncode != 0 or saida.get("is_error"):
        raise RuntimeError(saida.get("result") or proc.stderr.strip() or f"código {proc.returncode}")
    return saida.get("structured_output")


def main():
    db = supabase()
    fontes = (
        db.table("fontes").select("*").eq("usado", False)
        .order("publicado_em", desc=True).limit(POR_EXECUCAO).execute().data
    )
    if not fontes:
        print("[gerador] nenhuma fonte nova")
        return

    for fonte in fontes:
        try:
            saida = gerar_um(fonte)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            print(f"[gerador] erro em {fonte['url']}: {e}")
            continue

        # Marca como usada mesmo se descartada, para não gastar chamadas de novo.
        db.table("fontes").update({"usado": True}).eq("id", fonte["id"]).execute()

        if not saida or not saida["publicar"]:
            print(f"[gerador] pulado ({saida and saida['motivo']}): {fonte['titulo']}")
            continue
        tweet = saida["tweet"].strip()
        if len(tweet) > LIMITE_TWEET:
            print(f"[gerador] tweet com {len(tweet)} caracteres, descartado: {fonte['titulo']}")
            continue

        midia_tipo, midia_urls = resolver_midia(fonte, saida)
        db.table("rascunhos").insert({
            "fonte_id": fonte["id"],
            "texto": tweet,
            "link": fonte["url"],
            "status": "pendente",
            "midia_tipo": midia_tipo,
            "midia_urls": midia_urls,
        }).execute()
        print(f"[gerador] rascunho salvo ({midia_tipo or 'sem mídia'}): {tweet[:80]}…")


if __name__ == "__main__":
    main()
