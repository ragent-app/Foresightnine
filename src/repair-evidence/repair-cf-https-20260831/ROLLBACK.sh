#!/usr/bin/env bash
set -euo pipefail

API_BASE="https://api.cloudflare.com/client/v4"
ZONE_ID="5e79217ea213c033e4693b0f2b52d6ac"
TOKEN_FILE="/home/lxx/.cloudflared/key.txt"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base) API_BASE="$2"; shift 2 ;;
    --zone) ZONE_ID="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    *) printf 'usage: %s [--api-base URL] [--zone ID] [--token-file PATH]\n' "$0" >&2; exit 2 ;;
  esac
done

[[ -r "$TOKEN_FILE" ]] || { printf 'token file is unreadable\n' >&2; exit 3; }
TOKEN=$(cat "$TOKEN_FILE")
body=$(curl -sS --max-time 20 -X PATCH \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"value":"off"}' \
  -w '\nHTTP_STATUS:%{http_code}' \
  "${API_BASE%/}/zones/${ZONE_ID}/settings/always_use_https")
status=$(printf '%s\n' "$body" | sed -n 's/^HTTP_STATUS://p')
payload=$(printf '%s\n' "$body" | sed '/^HTTP_STATUS:/d')
value=$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("result") or {}).get("value", ""))
except Exception: print("")')
printf 'HTTP_STATUS=%s value=%s\n' "$status" "$value"
[[ "$status" = 200 && "$value" = off ]] || exit 1
