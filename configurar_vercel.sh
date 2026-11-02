#!/bin/bash
# Cadastra na Vercel as chaves do webhook do Telegram (lidas do .env, sem exibir).
set -e
cd "$(dirname "$0")"
if ! grep -q '^TELEGRAM_WEBHOOK_SECRET=' .env; then
  echo "TELEGRAM_WEBHOOK_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
fi
for k in SUPABASE_URL SUPABASE_SERVICE_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_WEBHOOK_SECRET \
         X_API_KEY X_API_SECRET X_ACCESS_TOKEN X_ACCESS_TOKEN_SECRET DRY_RUN; do
  v=$(grep -E "^$k=" .env | tail -1 | cut -d= -f2-)
  vercel env rm "$k" production --yes >/dev/null 2>&1 || true
  printf %s "$v" | vercel env add "$k" production >/dev/null
  echo "✓ $k"
done
# Token do GitHub para o bot disparar a geração na hora (/gerar e pedidos pelo chat).
vercel env rm GITHUB_TOKEN production --yes >/dev/null 2>&1 || true
gh auth token | tr -d '\n' | vercel env add GITHUB_TOKEN production >/dev/null
echo "✓ GITHUB_TOKEN"
echo "Pronto. Avise o Claude."
