#!/bin/sh
set -eu

BASE_URL="${AI_INDEX_BASE_URL:-https://index.shlab.org.cn/api/v2}"
BASE_URL=${BASE_URL%/}
API_KEY_ENV="AI_INDEX_API_KEY"
API_KEY_PAGE="https://index.shlab.org.cn/api-key"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

usage() {
  cat <<'EOF'
Usage:
  ai_index_openapi.sh check
  ai_index_openapi.sh COMMAND [JSON_OBJECT]
  ai_index_openapi.sh institution-funding INSTITUTION_ID

Commands:
  scholar-search       paper-search       institution-search
  institution-funding news-search        github-search
  huggingface-search  twitter-search     youtube-search
  hackernews-search

Examples:
  ai_index_openapi.sh institution-search '{"keyword":"OpenAI","page":1,"size":5}'
  ai_index_openapi.sh github-search '{"institutions":["OpenAI"],"sort_type":"heat","page":1,"size":10}'
EOF
}

load_api_key() {
  api_key=${AI_INDEX_API_KEY:-}
  if [ -z "$api_key" ]; then
    die "$API_KEY_ENV is not configured. Create or manage a key at $API_KEY_PAGE, configure it in the agent process environment, then rerun: $0 check. Do not paste the key into chat."
  fi
  if ! printf '%s' "$api_key" | LC_ALL=C grep -Eq '^ak_[0-9A-Za-z]{16}$'; then
    die "$API_KEY_ENV must match ak_ followed by exactly 16 ASCII letters or digits. Manage keys at $API_KEY_PAGE."
  fi
}

command_name=${1:-}
[ -n "$command_name" ] || {
  usage >&2
  exit 2
}

case "$command_name" in
  -h|--help|help)
    usage
    exit 0
    ;;
esac

# Managed direct invocations use exactly the same append-only registration path.
if [ "${command_name}" != check ] && [ -n "${DATAELF_AI_INDEX_QUERY_CONTEXT:-}" ] && [ "${DATAELF_AI_INDEX_RAW_CLIENT:-0}" != 1 ]; then
  exec "${DATAELF_PYTHON:-python}" -m dataelf.domains.ai_index.skill_tools "$DATAELF_AI_INDEX_QUERY_CONTEXT" direct "$@"
fi

load_api_key

if [ "$command_name" = "check" ]; then
  printf '%s is configured and has a valid format.\n' "$API_KEY_ENV"
  exit 0
fi

command -v curl >/dev/null 2>&1 || die "curl is required; Python and Node.js are not required"

method=POST
if [ "$#" -ge 2 ]; then
  body=$2
else
  body='{}'
fi
case "$command_name" in
  scholar-search) path=/openapi/scholar/search ;;
  paper-search) path=/openapi/paper/search ;;
  institution-search) path=/openapi/institutions/search ;;
  news-search) path=/openapi/news/search ;;
  github-search) path=/openapi/ecosystem/gh/search ;;
  huggingface-search) path=/openapi/ecosystem/hf/search ;;
  twitter-search) path=/openapi/ecosystem/xx/search ;;
  youtube-search) path=/openapi/ecosystem/y2b/search ;;
  hackernews-search) path=/openapi/ecosystem/hn/search ;;
  institution-funding)
    method=GET
    institution_id=${2:-}
    [ -n "$institution_id" ] || die "institution-funding requires INSTITUTION_ID"
    printf '%s' "$institution_id" | LC_ALL=C grep -Eq '^[0-9A-Za-z._~-]+$' || die "institution_id contains unsupported URL characters"
    path="/openapi/institutions/$institution_id/funding-profile"
    ;;
  *)
    usage >&2
    die "unknown command: $command_name"
    ;;
esac

# Feed the credential through curl's stdin config so it is not exposed in the
# process argument list. Never enable shell tracing around this script.
if [ "$method" = GET ]; then
  {
    printf 'header = "Accept: application/json"\n'
    printf 'header = "X-AI-Index-Key: %s"\n' "$api_key"
    printf 'header = "User-Agent: ai-index-search-skill/2.0"\n'
  } | curl --config - --silent --show-error --fail-with-body \
    --connect-timeout 30 --max-time "${AI_INDEX_REQUEST_TIMEOUT:-120}" \
    --write-out '%{stderr}DATAELF_HTTP_STATUS=%{http_code}\n' \
    --request GET "$BASE_URL$path"
else
  {
    printf 'header = "Accept: application/json"\n'
    printf 'header = "Content-Type: application/json"\n'
    printf 'header = "X-AI-Index-Key: %s"\n' "$api_key"
    printf 'header = "User-Agent: ai-index-search-skill/2.0"\n'
  } | curl --config - --silent --show-error --fail-with-body \
    --connect-timeout 30 --max-time "${AI_INDEX_REQUEST_TIMEOUT:-120}" \
    --write-out '%{stderr}DATAELF_HTTP_STATUS=%{http_code}\n' \
    --request POST "$BASE_URL$path" --data-binary "$body"
fi
printf '\n'
