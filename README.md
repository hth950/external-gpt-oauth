# external-gpt-oauth

FastAPI queue proxy for dogok. It accepts OpenAI-compatible requests on a
localhost-only port, stores them as durable SQLite jobs, and executes them
through a local `openai-oauth` proxy backed by a Codex/ChatGPT OAuth login.

## Dogok Setup

```bash
cd /workspace/thhwang/external-gpt-oauth
cp .env.example .env
# edit DOGOK_PROXY_API_KEY before starting

mkdir -p .codex-auth data logs
CODEX_HOME=/workspace/thhwang/external-gpt-oauth/.codex-auth npx @openai/codex login

sudo -n docker compose up -d --build
curl -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" http://127.0.0.1:31835/health
```

The service is exposed only on dogok loopback:

```bash
ssh -N -L 31835:127.0.0.1:31835 dogok
```

Then set clients to:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:31835/v1",
    api_key="DOGOK_PROXY_API_KEY value",
)
```

## Async Responses Flow

```bash
curl -s http://127.0.0.1:31835/v1/responses \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"hello"}'

curl -s http://127.0.0.1:31835/v1/responses/<response_id> \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY"
```

Statuses are `queued`, `in_progress`, `completed`, `failed`, or `cancelled`.

