"""2) Gera rascunhos de tweet em português, com mídia, e salva como "pendente".

Usa o Claude Code em modo não interativo (`claude -p`) com a sua assinatura
Pro/Max, então não consome crédito de API. Localmente usa o login do `claude`;
no GitHub Actions usa o CLAUDE_CODE_OAUTH_TOKEN gerado com `claude setup-token`.
"""
import json
import os
import random
import subprocess
from collections import Counter
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
        "gif_busca": {"type": "string", "description": "termo em inglês para buscar o GIF; vazio fora do formato gif"},
        "perfis": {
            "type": "array", "items": {"type": "string"},
            "description": "até 2 @ do X das empresas ou projetos centrais da notícia, no formato imagens",
        },
    },
    "required": ["publicar", "motivo", "tweet", "gif_busca", "perfis"],
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
        "- Each request names the post format. Write for that format; any media complements the text and "
        "is never described by it."
    )


FORMATOS = {
    "texto": "Text only, no media. The post must stand fully on its own.",
    "imagens": (
        "Text plus up to two images. For news about one or two well known companies or projects, give their "
        "exact X handles in perfis: each becomes a square card with its logo, and you may @mention them in "
        "the post, as @beincrypto does. Leave perfis empty when no company is central; the article image is "
        "used instead. For market data the image is a 7 day price chart."
    ),
    "gif": (
        "Text plus a reaction GIF. Write something light or ironic that the GIF can punctuate, and give a "
        "short English search term in gif_busca. If the story is too serious for a GIF (losses, hacks, people "
        "harmed), write it as text only and leave gif_busca empty."
    ),
}


def proximo_formato(recentes: list[str]) -> str:
    """Reveza os formatos: escolhe o menos usado entre os últimos rascunhos."""
    disponiveis = ["texto", "imagens"] + (["gif"] if os.getenv("GIPHY_API_KEY") else [])
    contagem = Counter(f for f in recentes[-9:] if f in disponiveis)
    menor = min(contagem[f] for f in disponiveis)
    return random.choice([f for f in disponiveis if contagem[f] == menor])


def formato_de(midia_tipo: str | None) -> str:
    return "texto" if not midia_tipo else "gif" if midia_tipo == "gif" else "imagens"


def resolver_midia(fonte: dict, formato: str, saida: dict) -> tuple[str | None, list[str] | None]:
    """Monta a mídia do formato pedido. Se nada der certo, o post sai só com texto."""
    try:
        if formato == "gif":
            url = buscar_gif(saida["gif_busca"])
            return ("gif", [url]) if url else (None, None)
        if formato != "imagens":
            return None, None
        if fonte["tipo"] == "mercado":
            png = grafico(fonte.get("dados") or {})
            return ("grafico", [salvar_publico(png, "png", "image/png")]) if png else (None, None)
        urls = [salvar_publico(c, "png", "image/png") for c in logos_do_x(saida["perfis"])]
        tipo = "logos" if urls else "materia"
        if len(urls) < 2:  # completa com a imagem da matéria
            imagem = fonte.get("imagem_url") or imagem_da_materia(fonte["url"])
            if imagem:
                urls.append(imagem)
        return (tipo, urls) if urls else (None, None)
    except Exception as e:  # mídia nunca deve impedir o rascunho
        print(f"[gerador] falha ao preparar a mídia ({formato}): {e}")
        return None, None


def gerar_um(fonte: dict, formato: str) -> dict | None:
    pedido = (
        f"Source type: {fonte['tipo']} (from {fonte.get('origem') or 'unknown'})\n"
        f"Source language: {fonte['idioma']}\n"
        f"Title: {fonte['titulo']}\n"
        f"Content: {fonte['resumo']}\n"
        f"Post format: {FORMATOS[formato]}\n\n"
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

    recentes = [
        formato_de(r["midia_tipo"]) for r in
        reversed(db.table("rascunhos").select("midia_tipo").order("id", desc=True).limit(9).execute().data)
    ]
    for fonte in fontes:
        formato = proximo_formato(recentes)
        try:
            saida = gerar_um(fonte, formato)
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

        midia_tipo, midia_urls = resolver_midia(fonte, formato, saida)
        recentes.append(formato_de(midia_tipo))
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
