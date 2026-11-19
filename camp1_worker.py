"""Worker do camp1-newsletter: roda os pedidos do site no Claude da assinatura.

O site (camp1-newsletter.vercel.app) grava o pedido em bot_estado com a chave camp1:job:<id>
(system, prompt e, se for curadoria, o JSON schema) e dispara o workflow camp1.yml. Aqui o
`claude -p` responde com o CLAUDE_CODE_OAUTH_TOKEN e o resultado volta para o mesmo registro.
Não imprime prompt nem resultado: o repo é público e o log do Actions também.
"""
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from config import supabase
from gerador import MODELO

PREFIXO = "camp1:job:"
GUARDAR_POR = timedelta(days=3)


def rodar(job: dict):
    args = ["claude", "-p", job["prompt"], "--model", MODELO, "--system-prompt", job["system"],
            "--output-format", "json", "--tools", "", "--strict-mcp-config", "--no-session-persistence"]
    if job.get("schema"):
        args += ["--json-schema", json.dumps(job["schema"])]
    ambiente = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600, env=ambiente)
    saida = json.loads(proc.stdout or "{}")
    if proc.returncode != 0 or saida.get("is_error"):
        raise RuntimeError(saida.get("result") or proc.stderr.strip()[:300] or f"código {proc.returncode}")
    return saida.get("structured_output") if job.get("schema") else (saida.get("result") or "").strip()


def atender(db, linha: dict):
    job = json.loads(linha["valor"])
    try:
        job.update(status="pronto", resultado=rodar(job))
    except Exception as e:
        job.update(status="erro", erro=str(e)[:500])
    # Sem o pedido inteiro de volta: guarda só o necessário para a tela.
    job.pop("prompt", None)
    job.pop("system", None)
    job.pop("schema", None)
    db.table("bot_estado").upsert({"chave": linha["chave"], "valor": json.dumps(job, ensure_ascii=False)}).execute()
    print(f"[camp1] {linha['chave'][len(PREFIXO):][:8]} {job['tipo']}: {job['status']}")


def main():
    db = supabase()
    # Repete enquanto chegarem pedidos novos durante a execução (o disparo do GitHub pode cair na fila).
    for _ in range(10):
        linhas = db.table("bot_estado").select("*").like("chave", f"{PREFIXO}%").execute().data
        pendentes, agora = [], datetime.now(timezone.utc)
        for l in linhas:
            job = json.loads(l["valor"])
            if job.get("status") == "pendente":
                pendentes.append(l)
            elif agora - datetime.fromisoformat(job["criado"].replace("Z", "+00:00")) > GUARDAR_POR:
                db.table("bot_estado").delete().eq("chave", l["chave"]).execute()
        if not pendentes:
            break
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda l: atender(db, l), pendentes))


if __name__ == "__main__":
    main()
