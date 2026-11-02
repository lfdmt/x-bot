#!/bin/bash
# Cadastra na Vercel as chaves do webhook do Telegram (lidas do .env, sem exibir).
set -e
cd "$(dirname "$0")"
if ! grep -q '^TELEGRAM_WEBHOOK_SECRET=' .env; then
  echo "TELEGRAM_WEBHOOK_SECRET=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')" >> .env
fi
for k in SUPABASE_URL SUPABASE_SERVICE_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_WEBHOOK_SECRET; do
  v=$(grep -E "^$k=" .env | tail -1 | cut -d= -f2-)
  vercel env rm "$k" production --yes >/dev/null 2>&1 || true
  printf %s "$v" | vercel env add "$k" production >/dev/null
  echo "✓ $k"
done
echo "Pronto. Avise o Claude."
