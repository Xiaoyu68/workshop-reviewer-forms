#!/usr/bin/env bash
# Deploy site/ to Vercel production. Needs VERCEL_ORG_ID and VERCEL_PROJECT_ID,
# from the environment or from a local .vercel.env file (git-ignored); in CI also
# VERCEL_TOKEN.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
[ -f "$here/.vercel.env" ] && set -a && . "$here/.vercel.env" && set +a
cd "$here/site"
if command -v vercel >/dev/null; then cli=(vercel); else cli=(npx --yes vercel@50.17.1); fi
"${cli[@]}" deploy --prod --yes ${VERCEL_TOKEN:+--token "$VERCEL_TOKEN"} >/dev/null 2>"$here/state/deploy.err" \
  || { cat "$here/state/deploy.err" >&2; exit 1; }
