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
POR_EXECUCAO = int(os.getenv("RASCUNHOS_POR_EXECUCAO", "1"))
# Relatório de 2026-11-10: ~6 posts de notícia por dia deixaram a conta com cara de portal e o alcance caiu.
# Conta só os rascunhos automáticos; o /gerar que você pede no Telegram sempre gera.
POR_DIA = int(os.getenv("RASCUNHOS_POR_DIA", "6"))
# Horário de Brasília em que a coleta automática gera rascunhos (de madrugada ela só guarda as notícias).
HORA_INICIO, HORA_FIM = 7, 23
# Notícias repetidas ou fora do tema são puladas: segue descendo a lista até este limite de chamadas.
MAX_TENTATIVAS = int(os.getenv("MAX_TENTATIVAS", "15"))
VOZ = Path(__file__).with_name("voz.md")
REFERENCIA = Path(__file__).with_name("referencia.md")
PILARES_IDS = [p["id"] for p in json.loads(Path(__file__).with_name("temas.json").read_text(encoding="utf-8"))["pilares"]]

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


# Posicionamento desde 2026-11-20: saiu do BeInCrypto. Nada pode apresentá-lo como funcionário de lá.
PERSONA = (
    "Luís de Magalhães (@LuisDeMaga): communication and brand reputation specialist, partner and advisor "
    "at Camp1 (a B2B PR agency for startups), formerly at BeInCrypto (where he was Head of LATAM and covered "
    "crypto from inside), BlaBlaCar and Samsung, with more than 10 years of TV journalism at Globo and Band. "
    "Never present him as currently working at BeInCrypto; if it matters, he is ex-BeInCrypto. Never make "
    "the post about himself or his agency: his background shows in the quality of the read, not in "
    "self promotion."
)

REGRAS_ESCRITA = (
    "- Never use em dashes, en dashes or hyphens as punctuation; use periods, commas, colons or parentheses.\n"
    "- Never use the 'não é X, é Y' construction or any variation ('não é sobre X, é sobre Y', 'X não é A. "
    "É B.', 'menos X, mais Y' as a contrast formula).\n"
    "- At most one hashtag, and only if it really makes sense (usually none). No decorative emojis.\n"
    "- Tone: direct and opinionated, a journalist who saw the market from inside. No lecture, no hype.\n"
)


def sem_travessao(texto: str) -> str:
    """Rede de segurança das regras de escrita: travessão ou hífen solto como pontuação vira vírgula."""
    for t in (" — ", " – ", " - "):
        texto = texto.replace(t, ", ")
    return texto.replace("—", ", ").replace("–", ", ").strip()


CASHTAG = (
    "- When the post names a publicly traded company, write its cashtag next to the first mention, e.g. "
    "'Nvidia ($NVDA)' or 'Tesla ($TSLA)', so X shows the price card. X only links cashtags made of "
    "letters (1 to 6), so B3 codes with digits like PETR4 do not work: for Brazilian companies use the "
    "US listed ticker when there is one (Petrobras $PBR, Vale $VALE, Itaú $ITUB, Nubank $NU, Bradesco "
    "$BBD, Ambev $ABEV, XP $XP, Inter $INTR, StoneCo $STNE, PagSeguro $PAGS, Sabesp $SBS, Eletrobras "
    "$EBR, Gerdau $GGB, Suzano $SUZ); otherwise no cashtag. X allows only ONE cashtag per post: use it "
    "for the most central company. Only use a ticker you are sure of; never invent one. Not for crypto "
    "tokens, private companies or government bodies.\n"
)


GANCHO = (
    "- The FIRST LINE is a human hook that makes people stop scrolling, written like a person talking, "
    "not like a headline. Pick what fits the story: a reaction ('Essa eu não esperava.'), something "
    "relatable from everyday Brazilian life (the bank app, the market, the boleto, the group chat, the "
    "salary), an ironic contrast, a striking number turned into something people feel, or a short "
    "provocative line. Keep it short (ideally under 70 characters) and true to the source. Never open "
    "with the institution's name followed by a verb, the way a portal headline does ('O Banco Central "
    "informou...', 'A Receita vai...'). No clickbait clichés: 'URGENTE', 'BOMBA', 'Você não vai "
    "acreditar', 'Atenção', 'Ninguém está falando sobre isso', 'Plot twist'. Vary the kind of hook "
    "from post to post."
)


def system_prompt() -> str:
    from temas import para_prompt
    return (
        f"You write posts for the personal X account of {PERSONA}\n\n"
        "The voice guide below defines tone and hard limits; follow it strictly.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        "The reference below shows formats that work for news on X, adapted to Portuguese. "
        "Use them as a repertoire; the voice guide still wins on tone.\n\n"
        f"<format_reference>\n{REFERENCIA.read_text(encoding='utf-8')}\n</format_reference>\n\n"
        f"The account's content pillars:\n{para_prompt(None)}\n\n"
        "Rules for every post:\n"
        f"- Brazilian Portuguese only, at most {LIMITE_TWEET - 10} characters. A short, strong single post.\n"
        "- No links or URLs, and do not tell readers to click or read the article: the post stands alone.\n"
        "- Sources come in Portuguese, Spanish or English. Always write in Portuguese, in your own words; "
        "never translate a source line by line.\n"
        "- Only state facts present in the source. Never invent numbers, quotes or predictions.\n"
        "- If the source fits none of the pillars, is minor, or touches a limit in the guide, set publicar=false.\n"
        f"{REGRAS_ESCRITA}"
        f"{GANCHO}\n"
        f"{CASHTAG}"
        "- After the hook, the fact, briefly, then his take in one sentence, from someone who covered "
        "tech and markets from inside and now works on communication and reputation: what it means for "
        "founders, builders, users or the brand involved, a contradiction, a comparison, what the coverage is "
        "missing, or how the company is telling (or botching) its story. Never a neutral headline. Never the "
        "'O detalhe:' / 'O motivo:' portal formula. A take is an opinion clearly framed as his, never a new fact.\n"
        "- When it fits naturally, connect to Brazil or Latin America (adoption, regulation, local startups, "
        "the real). Foreign stories are welcome when they matter to people who follow AI, Silicon Valley, startups and fintech.\n"
        "- Avoid AI tells in the take: 'é X, não Y', 'isso mostra que', 'vale lembrar', "
        "polished parallel sentences, lists of three.\n"
        "- Sometimes (about one post in three) end with a short, real question that invites replies, "
        "or take a clear side on something debatable. No generic 'o que vocês acham?'.\n"
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


def claude_json(pedido: str, sistema: str, schema: dict) -> dict | None:
    """Chama o Claude pela assinatura (`claude -p`) e devolve a saída validada pelo schema."""
    # Sem ANTHROPIC_API_KEY no ambiente, o claude usa a assinatura e não a API paga.
    ambiente = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(
        [
            "claude", "-p", pedido,
            "--model", MODELO,
            "--system-prompt", sistema,
            "--json-schema", json.dumps(schema),
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


# Alguns posts (não todos) contam de onde ele leu, como gente falando: "tava lendo isso na Bloomberg...".
CHANCE_LEITURA = float(os.getenv("CHANCE_LEITURA", "0.33"))
VEICULOS = {"Bloomberg Tech": "Bloomberg", "TechCrunch AI": "TechCrunch", "TechCrunch Venture": "TechCrunch",
            "TechCrunch Fintech": "TechCrunch", "BeInCrypto Brasil": "BeInCrypto", "MIT Tech Review Brasil": "MIT Tech Review"}


def veiculo(fonte: dict) -> str:
    origem = fonte.get("origem") or ""
    return VEICULOS.get(origem, origem)


def modo_leitura(fonte: dict) -> str:
    return (
        f"Write this one as something he just read: mention casually, like a person talking, that he read "
        f"it in {veiculo(fonte)} and found it interesting, then the fact and his take. Vary the wording, for "
        f"example 'Tava lendo isso na {veiculo(fonte)} e fiquei com uma coisa na cabeça', 'Po, li agora na "
        f"{veiculo(fonte)} e achei curioso', 'Essa da {veiculo(fonte)} hoje é boa'. Use the right article for "
        "the outlet (na Bloomberg, no Valor, na TechCrunch). Never say 'segundo a' or sound like a citation; "
        "do not copy these examples word for word, and never reuse the opening of any recent draft listed above. "
        "Still no link.\n\n"
    )


def gerar_um(fonte: dict, formato: str, ja_cobertos: list[str], angulo: str = "", pilar: str = "",
             leitura: bool | None = None) -> dict | None:
    if leitura is None:
        leitura = fonte.get("tipo") == "noticia" and bool(fonte.get("origem")) and random.random() < CHANCE_LEITURA
    cobertos = "\n".join(f"- {t.splitlines()[0][:140]}" for t in ja_cobertos[-25:]) or "- (none)"
    pedido = (
        "Stories already drafted recently. If this source is about the same event as any of them, "
        "even from another outlet or angle, set publicar=false:\n"
        f"{cobertos}\n\n"
        f"Source type: {fonte['tipo']} (from {fonte.get('origem') or 'unknown'})\n"
        f"Source language: {fonte['idioma']}\n"
        f"Title: {fonte['titulo']}\n"
        f"Content: {fonte['resumo']}\n"
        f"Post format: {FORMATOS[formato]}\n\n"
        + (f"Pillar: {pilar}\n" if pilar else "")
        + (f"Why the editor picked it (suggested angle): {angulo}\n\n" if angulo else "\n")
        + (modo_leitura(fonte) if leitura else "") +
        "Write one post about this."
    )
    return claude_json(pedido, system_prompt(), SCHEMA)


def gerar_pedido(db, instrucao: str, origem: str = "pedido seu no Telegram") -> int | None:
    """Rascunho a partir de um pedido escrito por você no Telegram. Devolve o id do rascunho."""
    recentes = [formato_de(r["midia_tipo"]) for r in
                db.table("rascunhos").select("midia_tipo").order("id", desc=True).limit(9).execute().data]
    # Sem matéria de origem, o formato imagens só consegue usar cards com logos.
    formato = proximo_formato(list(reversed(recentes)))
    pedido = (
        "The account owner asked for a post. Treat his message as the brief and as the only source of "
        "facts: do not add numbers, names or events he did not mention. publicar=false only if the request "
        "is impossible without inventing facts; explain why in motivo.\n\n"
        f"His request:\n\"\"\"\n{instrucao}\n\"\"\"\n\n"
        f"Post format: {FORMATOS[formato]}\n\nWrite one post."
    )
    saida = claude_json(pedido, system_prompt(), SCHEMA)
    if not saida or not saida["publicar"] or len(saida["tweet"].strip()) > LIMITE_TWEET:
        return None
    fonte = {"tipo": "noticia", "url": "", "imagem_url": None}
    midia_tipo, midia_urls = resolver_midia(fonte, formato, saida)
    linha = db.table("rascunhos").insert({
        "texto": sem_travessao(saida["tweet"]), "link": origem, "status": "pendente",
        "midia_tipo": midia_tipo, "midia_urls": midia_urls,
    }).execute().data
    return linha[0]["id"] if linha else None


SCHEMA_VIRAIS = {
    "type": "object",
    "properties": {
        "tweets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tema": {"type": "string"}, "tweet": {"type": "string"}},
                "required": ["tema", "tweet"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tweets"],
    "additionalProperties": False,
}


REFERENCIA_VIRAL = Path(__file__).with_name("referencia_viral.md")


def sistema_viral() -> str:
    return (
        "You write posts for the personal X account described below. Read who he is and how he sounds.\n\n"
        f"<reference>\n{REFERENCIA_VIRAL.read_text(encoding='utf-8')}\n</reference>\n\n"
        "What to write: a journalist commenting on real things that happened today, from his own point of "
        "view. Themes: Brazilian politics (Lula, Bolsonaro, Renan Santos, Congress, STF, Haddad), "
        "geopolitics (Trump, China, wars, tariffs, elections abroad), financial markets (Selic, dollar, "
        "Ibovespa, Fed, big moves) and innovation (Elon Musk, AI, big tech, regulation).\n"
        "Tone: a little funny, not a joke account. Sometimes critical, with dry irony, never angry. He "
        "criticizes whoever deserves it, left or right, and never sounds like a party activist. No cheering "
        "for a candidate, no insults, no calls to vote.\n\n"
        "Rules:\n"
        f"- Brazilian Portuguese, at most {LIMITE_TWEET - 10} characters, most under 220. Break lines between the hook, the fact and the take.\n"
        f"{GANCHO}\n"
        f"{CASHTAG}"
        "- After the hook, the specific fact from the headlines. The reader should know what happened "
        "from the tweet itself (name who and what), then get his take: an ironic observation, a sharp "
        "comparison, a question that exposes a contradiction, or what it means for the economy or Brazil.\n"
        "- Comment, never summarize: no 'X anunciou que' as the whole tweet, no neutral news report.\n"
        "- Never invent facts, numbers, quotes or events. Use only what the headlines say. Never put words "
        "in a real person's mouth.\n"
        "- No links, no hashtags, no decorative emoji.\n"
        "- Never use hyphens as punctuation and never the 'não é X, é Y' construction or its variations.\n"
        "- Crypto at most once in a while (the account already posts crypto news).\n"
        "- Never joke about deaths, victims, disasters, illness or suffering.\n"
        "- Write like he types: standard capitalization, light punctuation (no final period, no em dash, "
        "no semicolon), casual. Abbreviations only where natural (vc, q, pra, tá). Do not force slang.\n"
        "- Avoid AI tells: 'não é X, é Y', 'isso mostra que', 'vale lembrar', 'pelo menos agora', "
        "'só faltava', lists of three, polished parallel sentences, lecture tone.\n"
        "- tema is a 1 to 3 word label in Portuguese (e.g. 'Política', 'Geopolítica', 'Mercado', "
        "'Inovação', 'Trump', 'Musk')."
    )


def gerar_virais(db, conversa: str, tema: str = "", quantos: int = 3) -> list[int]:
    """Comentários do jornalista sobre fatos do dia. Devolve os ids dos rascunhos."""
    ja = [r["texto"] for r in db.table("rascunhos").select("texto").order("id", desc=True).limit(20).execute().data]
    pedido = (
        (f"Theme requested by the account owner: \"{tema}\". All tweets must be about it.\n\n" if tema else
         "All tweets must comment on facts from today's headlines, each on a different story. Mix the "
         "themes (politics, geopolitics, markets, innovation) and prefer the stories people are "
         "talking about most.\n\n") +
        "Headlines from the last hours (what is happening today; never summarize them as news):\n"
        f"{conversa or '(nothing collected; reply with an empty list)'}\n\n"
        "Recent drafts, do not repeat their ideas:\n" + "\n".join(f"- {t[:120]}" for t in ja) +
        f"\n\nWrite {quantos} tweets, each with a different idea."
    )
    saida = claude_json(pedido, sistema_viral(), SCHEMA_VIRAIS) or {}
    ids = []
    for t in saida.get("tweets", [])[:quantos]:
        from respostas import casual
        texto = casual(t["tweet"].strip())  # só tira travessão e ponto final
        if not texto or len(texto) > LIMITE_TWEET:
            continue
        linha = db.table("rascunhos").insert({
            "texto": texto, "link": f"viral: {t['tema']}", "status": "pendente",
            "midia_tipo": None, "midia_urls": None,
        }).execute().data
        if linha:
            ids.append(linha[0]["id"])
    return ids


SCHEMA_THREADS = {
    "type": "object",
    "properties": {
        "threads": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tema": {"type": "string", "description": "assunto da thread em até 6 palavras"},
                    "por_que": {"type": "string", "description": "uma frase: por que prende o leitor até o fim"},
                    "fontes": {"type": "array", "items": {"type": "integer"},
                               "description": "números das matérias usadas, da lista do pedido"},
                    "tweets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["tema", "por_que", "fontes", "tweets"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["threads"],
    "additionalProperties": False,
}


def sistema_thread(db=None) -> str:
    from temas import para_prompt
    return (
        f"You write X threads for the personal account of {PERSONA}\n\n"
        "The voice guide below defines his tone; follow it.\n\n"
        f"<voice_guide>\n{VOZ.read_text(encoding='utf-8')}\n</voice_guide>\n\n"
        f"The account's content pillars:\n{para_prompt(db)}\n\n"
        "What a thread is for: explain one subject so well that people read to the end, save it, share it and "
        "follow him for the next one. Long dwell time, not hot takes. Pick subjects where several stories, "
        "read together, tell a bigger story than any single headline: a trend, a mechanism people use but do "
        "not understand (how a hack happened, why a price moved, what a rule changes, how a product makes "
        "money), a before and after, or how a company told (or botched) its story.\n\n"
        "His lens: he covered tech, crypto and markets from inside a newsroom and now works on communication "
        "and reputation, so he can say what the coverage keeps missing, which story is bigger than it looks, "
        "and what the communication behind the move reveals. Use that lens in one or two tweets, never as "
        "self promotion.\n\n"
        "Structure (5 to 8 tweets in total):\n"
        "- Tweet 1 is the hook: a human first line (a surprising fact, a contradiction, a question everyone "
        "has) and a clear promise of what the reader will understand by the end. No 'Segue o fio', no "
        "'Thread:', no emoji, no clickbait clichés ('URGENTE', 'Ninguém está falando sobre isso').\n"
        "- The middle tweets build the explanation step by step: context, how it works, the numbers, who "
        "wins and who loses, what it means for founders, users or Latin America. Each tweet makes sense "
        "alone and makes the next one tempting; one idea per tweet.\n"
        "- The last tweet closes with his own read in one or two sentences and a natural invitation to follow "
        "him for more on the subject (e.g. 'Se curte esse tipo de explicação, me segue que eu sigo destrinchando "
        "por aqui'). Vary the wording; no 'RT se ajudou'.\n\n"
        "Rules:\n"
        f"- Brazilian Portuguese, each tweet at most {LIMITE_TWEET - 30} characters (count them: X cuts at "
        f"{LIMITE_TWEET}). No numbering like '1/7' (the bot adds it). No links.\n"
        f"{REGRAS_ESCRITA}"
        "- Only facts, numbers and quotes that appear in the stories given. Never invent. When explaining a "
        "general concept (what a stablecoin is, how a funding round works) you may use common knowledge, "
        "stated plainly and without new numbers.\n"
        f"{CASHTAG}"
        "- Avoid AI tells: 'isso mostra que', 'vale lembrar', 'em resumo', 'o ponto é', "
        "lists of three, polished parallel sentences, lecture tone. Write like a smart person explaining "
        "to a friend.\n"
        "- Each thread uses a different subject, preferably from different pillars, favouring the pillars "
        "with more weight. In fontes, list the numbers of the stories it is based on."
    )


def gerar_threads(db, tema: str = "", quantas: int = 2) -> list[dict]:
    """Sugestões de thread explicativa a partir das matérias de todos os feeds dos últimos 5 dias."""
    from datetime import datetime, timedelta, timezone
    from temas import pilares
    desde = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    materias = (
        db.table("fontes").select("titulo,resumo,url,publicado_em,origem")
        .eq("tipo", "noticia").gte("publicado_em", desde)
        .order("publicado_em", desc=True).limit(160).execute().data
    )
    if not materias:
        return []
    lista = "\n".join(
        f"[{i}] ({(m.get('publicado_em') or '')[:10]}, {m['origem']}) {m['titulo']}: {(m.get('resumo') or '')[:220]}"
        for i, m in enumerate(materias, 1)
    )
    pesos = ", ".join(f"{p['nome']} {p['peso']}%" for p in pilares(db))
    feitas = db.table("bot_estado").select("valor").eq("chave", "threads:temas").execute().data
    anteriores = json.loads(feitas[0]["valor"]) if feitas else []
    pedido = (
        (f"Subject requested by the account owner: \"{tema}\". Every thread must be about it, using the "
         "stories that touch it.\n\n" if tema else
         "Choose the subjects with the most explanatory potential for people who follow AI, Silicon "
         f"Valley, startups and fintech, never stock market or crypto price stories. Current pillar weights: {pesos}.\n\n") +
        f"Stories from the last 5 days:\n{lista}\n\n" +
        ("Subjects of threads already suggested, do not repeat:\n" + "\n".join(f"- {t}" for t in anteriores) + "\n\n"
         if anteriores else "") +
        f"Write {quantas} threads."
    )
    saida = claude_json(pedido, sistema_thread(db), SCHEMA_THREADS) or {}
    threads = []
    for t in saida.get("threads", [])[:quantas]:
        tweets = [sem_travessao(x) for x in t["tweets"] if x.strip()][:8]
        if len(tweets) < 5:
            continue
        t["tweets"] = tweets
        t["materias"] = [materias[n - 1] for n in t.get("fontes", []) if 0 < n <= len(materias)]
        threads.append(t)
    if threads:
        temas = (anteriores + [t["tema"] for t in threads])[-15:]
        db.table("bot_estado").upsert({"chave": "threads:temas", "valor": json.dumps(temas, ensure_ascii=False)}).execute()
    return threads


SCHEMA_CURADORIA = {
    "type": "object",
    "properties": {
        "escolhidas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "número da notícia na lista"},
                    "pilar": {"type": "string", "enum": PILARES_IDS},
                    "nota": {"type": "integer", "description": "0 a 10: quanto rende post forte e seguidor novo agora"},
                    "angulo": {"type": "string", "description": "a leitura própria do Luís em uma frase, em português"},
                },
                "required": ["n", "pilar", "nota", "angulo"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["escolhidas"],
    "additionalProperties": False,
}


def sistema_curadoria(db) -> str:
    from temas import para_prompt
    return (
        f"You are the editor of the personal X account of {PERSONA}\n\n"
        "Read the latest stories and pick the ones that make the best posts for him: the kind that makes "
        "people who follow AI, Silicon Valley, startups and fintech think 'I need to follow this guy'.\n\n"
        f"His content pillars:\n{para_prompt(db)}\n\n"
        "For each story you pick:\n"
        "1. pilar: the ONE pillar it fits best. 'pontes' is only for stories that really join the two worlds "
        "(a big tech moving into crypto, AI applied to finance, investment in Latin American startups). "
        "'comunicacao' is for PR mistakes, brand crises, reputation turnarounds and how companies tell "
        "their story, including when a tech or crypto story is above all a communication story.\n"
        "2. nota 0 to 10, how hot it is right now: 9 or 10 if everyone is talking about it (big names, a "
        "number that impresses, a controversy, an unexpected turn, a lesson founders and investors want to "
        "share); 6 to 8 if interesting and current; 5 or less if cold or minor.\n"
        "3. angulo: his own read in one sentence, from someone who covered the market from inside and now "
        "works on communication and reputation. Not a summary of the news. No dashes, no semicolons, no "
        "'não é X, é Y' construction or variations: state it directly.\n\n"
        "The account is about INNOVATION first: AI, Silicon Valley, big tech, founders, startups, venture "
        "capital and fintech products. It is NOT a financial markets account.\n"
        "Prefer: new AI models, products and agents and the race between AI labs, big tech strategy moves, "
        "founders with strong stories, Silicon Valley culture and power plays, big rounds and exits as "
        "startup milestones, fintech products (payments, Pix, open finance, digital banks, stablecoins as a "
        "product), AI applied to banks and finance, reputation crises and turnarounds, surprising numbers "
        "about adoption or usage.\n"
        "Avoid (score 3 or less): stock market moves, indexes, sell offs and rallies, how a stock traded after "
        "an IPO, ETF flows, crypto prices and price predictions, macro, interest rates and central banks, "
        "earnings without a product story, analyst calls, exchange marketing and trading campaigns, niche "
        "tokens and protocols, listicles, events and agenda notes, sponsored content, partisan politics.\n"
        "Several outlets covering the same event means it is hot: pick it once, on the best source, even if "
        "the stories take different angles on it (at most two entries per big event, only if the angles are "
        "really different). Skip "
        "stories already covered in recent drafts. Return up to 25 candidates, with every pillar "
        "represented when the list allows."
    )


def curar(db, fontes: list[dict], ja_cobertos: list[str], tema: str = "") -> list[dict]:
    """Uma chamada que classifica as notícias por pilar e nota. Devolve dicts com fonte, pilar, nota e angulo."""
    lista = "\n".join(
        f"[{i}] ({f.get('origem')}, {(f.get('publicado_em') or '')[:16]}) {f['titulo']}: {(f.get('resumo') or '')[:220]}"
        for i, f in enumerate(fontes, 1)
    )
    cobertos = "\n".join(f"- {t.splitlines()[0][:140]}" for t in ja_cobertos[-25:]) or "- (none)"
    foco = (f"The account owner asked for posts about \"{tema}\": pick only stories about it.\n\n" if tema else "")
    saida = claude_json(f"{foco}Recent drafts:\n{cobertos}\n\nStories:\n{lista}",
                        sistema_curadoria(db), SCHEMA_CURADORIA) or {}
    vistas, ordem = set(), []
    for e in sorted(saida.get("escolhidas", []), key=lambda e: -e["nota"]):
        if 0 < e["n"] <= len(fontes) and e["n"] not in vistas:
            vistas.add(e["n"])
            ordem.append({"fonte": fontes[e["n"] - 1], "pilar": e["pilar"], "nota": e["nota"], "angulo": e["angulo"]})
    return ordem


def ranking(db, candidatos: list[dict], total: int = 10) -> list[dict]:
    """O top 10 do jeito do LinkedIn, com a fatia de cada pilar proporcional ao peso.
    Se um pilar não tiver matéria boa, a vaga vai para a melhor das outras."""
    from temas import cotas, pilares
    cota = cotas(pilares(db), total)
    escolhidos = []
    for pilar, n in cota.items():
        escolhidos += [c for c in candidatos if c["pilar"] == pilar][:n]
    sobra = [c for c in candidatos if c not in escolhidos]
    escolhidos += sobra[:total - len(escolhidos)]
    return sorted(escolhidos, key=lambda c: -c["nota"])[:total]


def fontes_recentes(db, horas: int, so_novas: bool = True) -> list[dict]:
    from datetime import datetime, timedelta, timezone
    desde = (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat()
    q = db.table("fontes").select("*").gte("publicado_em", desde)
    if so_novas:
        q = q.eq("usado", False)
    return q.order("publicado_em", desc=True).limit(150).execute().data


def contexto_recente(db) -> tuple[list[str], list[str]]:
    """(formatos dos últimos rascunhos, textos dos últimos rascunhos), do mais velho para o mais novo."""
    linhas = list(reversed(db.table("rascunhos").select("midia_tipo,texto").order("id", desc=True).limit(25).execute().data))
    return [formato_de(r["midia_tipo"]) for r in linhas[-9:]], [r["texto"] for r in linhas]


def pilares_recentes(db) -> list[str]:
    linha = db.table("bot_estado").select("valor").eq("chave", "pilares:recentes").execute().data
    return json.loads(linha[0]["valor"]) if linha else []


def lembrar_pilar(db, pilar: str):
    lista = (pilares_recentes(db) + [pilar])[-20:]
    db.table("bot_estado").upsert({"chave": "pilares:recentes", "valor": json.dumps(lista)}).execute()


def escrever(db, fonte: dict, angulo: str, pilar: str, recentes: list[str], ja_cobertos: list[str]) -> int | None:
    """Escreve o rascunho de uma matéria e salva como pendente. Devolve o id ou None."""
    formato = proximo_formato(recentes)
    try:
        saida = gerar_um(fonte, formato, ja_cobertos, angulo, pilar)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"[gerador] erro em {fonte['url']}: {e}")
        return None
    # Marca como usada mesmo se descartada, para não gastar chamadas de novo.
    db.table("fontes").update({"usado": True}).eq("id", fonte["id"]).execute()
    if not saida or not saida["publicar"]:
        print(f"[gerador] pulado ({saida and saida['motivo']}): {fonte['titulo']}")
        return None
    tweet = sem_travessao(saida["tweet"])
    if len(tweet) > LIMITE_TWEET:
        print(f"[gerador] tweet com {len(tweet)} caracteres, descartado: {fonte['titulo']}")
        return None
    midia_tipo, midia_urls = resolver_midia(fonte, formato, saida)
    recentes.append(formato_de(midia_tipo))
    ja_cobertos.append(tweet)
    linha = db.table("rascunhos").insert({
        "fonte_id": fonte["id"], "texto": tweet, "link": fonte["url"], "status": "pendente",
        "midia_tipo": midia_tipo, "midia_urls": midia_urls,
    }).execute().data
    if pilar:
        lembrar_pilar(db, pilar)
    print(f"[gerador] rascunho salvo ({pilar}, {midia_tipo or 'sem mídia'}): {tweet[:80]}…")
    return linha[0]["id"] if linha else None


def main():
    db = supabase()
    from datetime import datetime, timedelta, timezone
    from temas import mais_atrasado, pilares
    # Só o que saiu nas últimas 36 h: notícia velha não é quente.
    fontes = fontes_recentes(db, 36)
    if not fontes:
        print("[gerador] nenhuma fonte nova")
        return

    recentes, ja_cobertos = contexto_recente(db)
    inicio = datetime.now(timezone(timedelta(hours=-3))).replace(hour=0, minute=0, second=0, microsecond=0)
    hoje = db.table("rascunhos").select("id").not_.is_("fonte_id", "null").gte("criado_em", inicio.isoformat()).execute().data
    pedido = db.table("bot_estado").select("valor").eq("chave", "gerar:pedido").execute().data
    manual = bool(pedido) and datetime.now(timezone.utc) - datetime.fromisoformat(pedido[0]["valor"]) < timedelta(minutes=30)
    por_execucao = POR_EXECUCAO
    if manual:
        db.table("bot_estado").delete().eq("chave", "gerar:pedido").execute()
        # /investidor pede 3 rascunhos de uma vez.
        qtd = db.table("bot_estado").select("valor").eq("chave", "gerar:quantos").execute().data
        if qtd:
            db.table("bot_estado").delete().eq("chave", "gerar:quantos").execute()
            por_execucao = int(qtd[0]["valor"])
    else:
        hora = datetime.now(timezone(timedelta(hours=-3))).hour
        if not HORA_INICIO <= hora < HORA_FIM:
            print(f"[gerador] {hora}h em Brasília: fora do horário, só guardei as notícias")
            return
        if len(hoje) >= POR_DIA:
            print(f"[gerador] já são {len(hoje)} rascunhos de notícia hoje (limite {POR_DIA})")
            return
    try:
        candidatos = curar(db, fontes, ja_cobertos)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        print(f"[gerador] curadoria falhou ({e}); nada gerado nesta rodada")
        return
    # Automático só gera do que é quente de verdade (7+); pedido seu aceita a partir de 6.
    corte = 6 if manual else 7
    candidatos = [c for c in candidatos if c["nota"] >= corte]
    for c in candidatos:
        print(f"[gerador] {c['pilar']} nota {c['nota']}: {c['fonte']['titulo'][:80]} | {c['angulo']}")
    if not candidatos:
        print("[gerador] nada quente agora")
        return
    # O pilar mais abaixo do peso nos últimos rascunhos vai primeiro; dentro dele, a maior nota.
    ordem = mais_atrasado(pilares(db), pilares_recentes(db))
    fila = sorted(candidatos, key=lambda c: (ordem.index(c["pilar"]) if c["pilar"] in ordem else 99, -c["nota"]))
    criados = 0
    for c in fila[:MAX_TENTATIVAS]:
        if criados >= por_execucao:
            break
        if escrever(db, c["fonte"], c["angulo"], c["pilar"], recentes, ja_cobertos):
            criados += 1


if __name__ == "__main__":
    main()
