"""Mídia dos tweets: imagem da matéria, gráfico de preço, GIF e upload para o X."""
import io
import os
import re
import time
import uuid

import requests
from requests_oauthlib import OAuth1

from config import br, env, supabase

UA = {"User-Agent": "Mozilla/5.0 (x-bot)"}
BUCKET = "midia"
API_MIDIA = "https://api.x.com/2/media/upload"


def imagem_da_materia(url: str) -> str | None:
    """Imagem principal (og:image) da página da matéria."""
    if not url.startswith("http"):
        return None
    try:
        pagina = requests.get(url, headers=UA, timeout=30).text
    except requests.RequestException:
        return None
    achado = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', pagina
    ) or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']', pagina)
    return achado.group(1) if achado else None


def buscar_gif(termo: str) -> str | None:
    chave = os.getenv("GIPHY_API_KEY")
    if not chave or not termo:
        return None
    try:
        dados = requests.get("https://api.giphy.com/v1/gifs/search", timeout=30, params={
            "api_key": chave, "q": termo, "limit": 1, "rating": "g", "lang": "en",
        }).json()["data"]
        return dados[0]["images"]["downsized"]["url"] if dados else None
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


def grafico(dados: dict) -> bytes | None:
    """Gráfico de 7 dias do ativo, em PNG."""
    serie = dados.get("serie") or []
    if len(serie) < 2:
        return None
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from datetime import datetime, timezone

    xs = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t, _ in serie]
    ys = [p for _, p in serie]
    cor = "#16c784" if ys[-1] >= ys[0] else "#ea3943"

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=100)
    fig.patch.set_facecolor("#0f1115")
    ax.set_facecolor("#0f1115")
    ax.plot(xs, ys, color=cor, linewidth=3)
    ax.fill_between(xs, ys, min(ys), color=cor, alpha=0.12)
    for lado in ("top", "right", "left", "bottom"):
        ax.spines[lado].set_visible(False)
    ax.tick_params(colors="#9aa0a6", labelsize=13)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"US$ {br(v, 0)}"))
    ax.grid(axis="y", color="#2a2e35", linewidth=1)
    sinal = "+" if dados["variacao"] >= 0 else ""
    fig.text(0.06, 0.92, f"{dados['nome']} {dados['cashtag']}", color="white", fontsize=26, weight="bold")
    fig.text(0.06, 0.86, f"{sinal}{br(dados['variacao'])}% em 24h   ·   últimos 7 dias", color=cor, fontsize=16)
    fig.text(0.94, 0.03, "Dados: CoinGecko", color="#5f6368", fontsize=11, ha="right")
    fig.subplots_adjust(left=0.12, right=0.95, top=0.8, bottom=0.1)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


def salvar_publico(conteudo: bytes, extensao: str, tipo: str) -> str:
    """Guarda no Storage do Supabase e devolve a URL pública."""
    db = supabase()
    try:
        db.storage.create_bucket(BUCKET, options={"public": True})
    except Exception:
        pass  # já existe
    caminho = f"{uuid.uuid4().hex}.{extensao}"
    db.storage.from_(BUCKET).upload(caminho, conteudo, {"content-type": tipo})
    return db.storage.from_(BUCKET).get_public_url(caminho)


def enviar_para_x(url: str) -> str:
    """Baixa a mídia e sobe para o X. Devolve o media_id."""
    resp = requests.get(url, headers=UA, timeout=60)
    resp.raise_for_status()
    conteudo = resp.content
    tipo = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    auth = OAuth1(env("X_API_KEY"), env("X_API_SECRET"), env("X_ACCESS_TOKEN"), env("X_ACCESS_TOKEN_SECRET"))

    if tipo != "image/gif":
        r = requests.post(API_MIDIA, auth=auth, timeout=120,
                          files={"media": ("midia", conteudo, tipo)}, data={"media_category": "tweet_image"})
        r.raise_for_status()
        return r.json()["data"]["id"]

    # GIF exige upload em partes: initialize, append, finalize.
    r = requests.post(f"{API_MIDIA}/initialize", auth=auth, timeout=60, json={
        "media_type": "image/gif", "total_bytes": len(conteudo), "media_category": "tweet_gif",
    })
    r.raise_for_status()
    media_id = r.json()["data"]["id"]
    parte = 4 * 1024 * 1024
    for i in range(0, len(conteudo), parte):
        r = requests.post(f"{API_MIDIA}/{media_id}/append", auth=auth, timeout=120,
                          files={"media": ("midia", conteudo[i:i + parte], "image/gif")},
                          data={"segment_index": i // parte})
        r.raise_for_status()
    r = requests.post(f"{API_MIDIA}/{media_id}/finalize", auth=auth, timeout=60)
    r.raise_for_status()
    info = r.json()["data"].get("processing_info")
    while info and info.get("state") in ("pending", "in_progress"):
        time.sleep(info.get("check_after_secs", 2))
        r = requests.get(API_MIDIA, auth=auth, timeout=60, params={"command": "STATUS", "media_id": media_id})
        r.raise_for_status()
        info = r.json()["data"].get("processing_info")
    if info and info.get("state") == "failed":
        raise RuntimeError(f"X recusou o GIF: {info}")
    return media_id


def logos_do_x(perfis: list[str]) -> list[bytes]:
    """Um card quadrado escuro com o logo (foto de perfil) de cada perfil do X, como faz o @beincrypto."""
    from PIL import Image

    from publicador import cliente_x

    perfis = [p.lstrip("@").strip() for p in perfis if p.strip()][:2]
    if not perfis:
        return []
    from config import supabase

    db = supabase()
    fotos = {l["chave"].split(":", 1)[1]: l["valor"] for l in
             db.table("bot_estado").select("chave,valor").in_("chave", [f"foto_x:{p.lower()}" for p in perfis]).execute().data}
    faltam = [p for p in perfis if p.lower() not in fotos]
    from config import x_gratis
    if faltam and not x_gratis():  # ler perfil no X é pago: cada conta só é consultada uma vez
        resp = cliente_x().get_users(usernames=faltam, user_fields=["profile_image_url"], user_auth=True)
        for u in resp.data or []:
            fotos[u.username.lower()] = (u.profile_image_url or "").replace("_normal", "_400x400")
            db.table("bot_estado").upsert({"chave": f"foto_x:{u.username.lower()}", "valor": fotos[u.username.lower()]}).execute()
    cards = []
    for usuario in perfis:
        url = fotos.get(usuario.lower(), "")
        if not url:
            continue
        logo = Image.open(io.BytesIO(requests.get(url, headers=UA, timeout=30).content)).convert("RGBA")
        logo = logo.resize((640, 640), Image.LANCZOS)
        # O fundo do card usa a cor da borda do logo, para ele se fundir ao fundo.
        r, g, b, a = logo.getpixel((4, 4))
        fundo = (r, g, b) if a > 200 else (27, 27, 27)
        card = Image.new("RGB", (1080, 1080), fundo)
        card.paste(logo, (220, 220), logo)
        buf = io.BytesIO()
        card.save(buf, format="PNG")
        cards.append(buf.getvalue())
    return cards
