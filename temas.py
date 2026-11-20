"""Pilares do conteúdo e seus pesos. Os valores iniciais ficam em temas.json; o /temas do Telegram
grava um ajuste por cima (bot_estado, chave temas:pesos), e /temas padrao volta para o arquivo."""
import json
import unicodedata
from pathlib import Path

ARQUIVO = Path(__file__).with_name("temas.json")
CHAVE = "temas:pesos"


def pilares(db=None) -> list[dict]:
    """Os pilares com o peso em vigor (o do Telegram, se houver, senão o do arquivo)."""
    lista = json.loads(ARQUIVO.read_text(encoding="utf-8"))["pilares"]
    if db is not None:
        linha = db.table("bot_estado").select("valor").eq("chave", CHAVE).execute().data
        if linha:
            ajuste = json.loads(linha[0]["valor"])
            for p in lista:
                p["peso"] = ajuste.get(p["id"], p["peso"])
    return lista


def nome(pilar_id: str) -> str:
    return next((p["nome"] for p in pilares() if p["id"] == pilar_id), pilar_id)


def cotas(lista: list[dict], total: int) -> dict[str, int]:
    """Quantos itens de cada pilar num ranking de `total`, proporcional ao peso (maiores restos)."""
    soma = sum(max(p["peso"], 0) for p in lista) or 1
    brutas = {p["id"]: total * max(p["peso"], 0) / soma for p in lista}
    saida = {k: int(v) for k, v in brutas.items()}
    for k in sorted(brutas, key=lambda k: (brutas[k] - saida[k], brutas[k]), reverse=True):
        if sum(saida.values()) >= total:
            break
        if brutas[k] > 0:
            saida[k] += 1
    return saida


def mais_atrasado(lista: list[dict], recentes: list[str]) -> list[str]:
    """Pilares em ordem de prioridade: o que está mais abaixo do peso nos últimos rascunhos vem primeiro."""
    soma = sum(max(p["peso"], 0) for p in lista) or 1
    n = len(recentes) or 1
    deficit = {p["id"]: p["peso"] / soma - recentes.count(p["id"]) / n for p in lista if p["peso"] > 0}
    return sorted(deficit, key=lambda k: -deficit[k])


def _sem_acento(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def achar(db, termo: str) -> dict | None:
    """Pilar pelo número (1 a 4), pelo id ou por um pedaço do nome."""
    lista = pilares(db)
    if termo.isdigit() and 1 <= int(termo) <= len(lista):
        return lista[int(termo) - 1]
    t = _sem_acento(termo)
    return next((p for p in lista if t in (p["id"], _sem_acento(p["nome"])) or t in _sem_acento(p["nome"])), None)


def texto_temas(db) -> str:
    lista = pilares(db)
    linhas = ["🎯 Pilares e pesos em vigor:\n"]
    for i, p in enumerate(lista, 1):
        linhas.append(f"{i}. {p['nome']}: {p['peso']}%\n   {p['descricao']}")
    soma = sum(p["peso"] for p in lista)
    if soma != 100:
        linhas.append(f"\n(Soma {soma}%. O bot usa as proporções, então tudo bem não fechar 100.)")
    linhas.append("\nPara mudar: /temas cripto 30 (ou /temas 2 30). Para voltar ao temas.json: /temas padrao")
    return "\n".join(linhas)


def ajustar(db, termo: str, peso: int) -> str:
    p = achar(db, termo)
    if not p:
        return f"Não achei o pilar \"{termo}\". Use o número (1 a 4) ou parte do nome."
    if not 0 <= peso <= 100:
        return "O peso vai de 0 a 100."
    linha = db.table("bot_estado").select("valor").eq("chave", CHAVE).execute().data
    ajuste = json.loads(linha[0]["valor"]) if linha else {}
    ajuste[p["id"]] = peso
    db.table("bot_estado").upsert({"chave": CHAVE, "valor": json.dumps(ajuste)}).execute()
    return f"✅ {p['nome']} agora pesa {peso}%.\n\n" + texto_temas(db)


def padrao(db) -> str:
    db.table("bot_estado").delete().eq("chave", CHAVE).execute()
    return "✅ Pesos de volta aos do temas.json.\n\n" + texto_temas(db)


def para_prompt(db) -> str:
    """Os pilares como o Claude lê na curadoria."""
    return "\n".join(f"- {p['id']}: {p['nome']} ({p['descricao']})" for p in pilares(db))
