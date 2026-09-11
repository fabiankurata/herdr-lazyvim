#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"

failed=false

check_contents() {
  label=$1
  pattern=$2
  if git grep -nIE "$pattern" -- . ':(exclude)scripts/check-public.sh'; then
    printf 'public audit failed: %s\n' "$label" >&2
    failed=true
  fi
}

private_project="mishop""pu"
private_machine="todai""ji"
check_contents "private project or machine identifier" "$private_project|$private_machine|/Users/[A-Za-z0-9._-]+"
check_contents "private key" 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'
check_contents "GitHub token" 'gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}'
check_contents "OpenAI-style token" 'sk-[A-Za-z0-9_-]{20,}'
check_contents "AWS access key" 'AKIA[0-9A-Z]{16}'

if git ls-files | grep -E '(^|/)(\.env($|\.)|id_(rsa|ed25519)($|\.)|.*\.(pem|p12|key)$)' >/dev/null; then
  git ls-files | grep -E '(^|/)(\.env($|\.)|id_(rsa|ed25519)($|\.)|.*\.(pem|p12|key)$)' >&2
  printf 'public audit failed: sensitive filename\n' >&2
  failed=true
fi

private_email="fabiankurata""@gmail.com"
if git log --all --format='%ae%n%ce' | grep -Fi "$private_email" >/dev/null; then
  printf 'public audit failed: personal email remains in reachable Git history\n' >&2
  failed=true
fi

if [ "$failed" = true ]; then
  exit 1
fi

printf 'public audit: ok\n'
