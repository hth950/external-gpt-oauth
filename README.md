# external-gpt-oauth

dogok 서버에서 ChatGPT/Codex OAuth 계정으로 GPT 요청을 처리하는 FastAPI 기반 내부 프록시입니다.

클라이언트는 dogok의 LAN 주소로 OpenAI 호환 요청을 보내고, 이 서버는 요청을 SQLite queue에 저장한 뒤 내부 `openai-oauth` 프록시를 통해 처리합니다. 긴 GPT 응답 때문에 HTTP 연결을 오래 붙잡지 않도록 `/v1/responses` 요청은 즉시 job id를 반환하고, 클라이언트가 poll로 결과를 조회하는 구조입니다.

## Current Endpoint

현재 dogok 배포 기준:

```text
Base URL: http://192.168.0.16:31835/v1
API Key: dogok .env의 DOGOK_PROXY_API_KEY
```

클라이언트 쉘에서 예제를 실행할 때는 먼저 실제 key를 환경변수로 지정합니다.

```bash
export DOGOK_PROXY_API_KEY="실제 .env 값"
```

헬스체크:

```bash
curl http://192.168.0.16:31835/health \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY"
```

Swagger/OpenAPI는 외부 공개를 고려해 기본 비활성화되어 있습니다. 필요한 경우 dogok `.env`에서 `DOGOK_PROXY_ENABLE_DOCS=true`를 임시로 설정하고 재시작한 뒤 아래 URL을 사용합니다.

```text
http://192.168.0.16:31835/docs
```

Swagger에서 우측 상단 `Authorize` 버튼을 눌러 실제 API key를 입력하면 됩니다. `/v1/responses`의 request body 예시에 `system`/`usr` payload가 포함되어 있습니다. 확인이 끝나면 `DOGOK_PROXY_ENABLE_DOCS=false`로 되돌립니다.

## Quick Usage

### Async Queue Behavior

요청 API는 비동기 queue 방식입니다. 사용자가 동시에 여러 요청을 보내도 서버는 각 요청을 SQLite queue에 저장하고 즉시 `id`를 반환합니다. 실제 GPT 호출은 background worker가 제한된 동시성으로 처리합니다.

현재 운영 기본값:

```text
GPT_QUEUE_CONCURRENCY=2
```

예를 들어 요청이 10개 동시에 들어오면:

```text
10개 요청 저장
2개 in_progress 처리
8개 queued 대기
처리 중이던 작업이 끝나면 다음 queued job 처리
```

따라서 사용자는 생성 응답의 `id`를 저장하고, `/v1/responses/{id}`를 poll해서 `completed`가 될 때 결과를 읽으면 됩니다.

### Responses API

가장 단순한 형태는 `system`과 `usr`를 나누는 방식입니다.

```bash
CREATE=$(curl -sS http://192.168.0.16:31835/v1/responses \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4-mini",
    "system": "You are a concise assistant.",
    "usr": "Reply with exactly: hello"
  }')
```

지원하는 alias:

```text
system prompt: system, system_prompt
user prompt: usr, user, user_prompt, prompt
```

요청 생성:

```bash
CREATE=$(curl -sS http://192.168.0.16:31835/v1/responses \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4-mini","input":"Reply with exactly: hello"}')

echo "$CREATE"
```

결과 poll:

```bash
RID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$CREATE")

curl -sS "http://192.168.0.16:31835/v1/responses/${RID}" \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" | python3 -m json.tool
```

상태값은 `queued`, `in_progress`, `completed`, `failed`, `cancelled` 중 하나입니다. 완료 시 `output_text`와 Responses 형태의 `output`이 반환됩니다.

OpenAI Responses 스타일의 role list도 사용할 수 있습니다.

```bash
curl -sS http://192.168.0.16:31835/v1/responses \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.4-mini",
    "input": [
      {"role": "system", "content": "You are a concise assistant."},
      {"role": "user", "content": "Reply with exactly: hello"}
    ]
  }'
```

### Python Client

```python
import os
import time
import requests

BASE_URL = "http://192.168.0.16:31835"
API_KEY = os.environ["DOGOK_PROXY_API_KEY"]
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

created = requests.post(
    f"{BASE_URL}/v1/responses",
    headers=HEADERS,
    json={
        "model": "gpt-5.4-mini",
        "system": "You are a concise assistant.",
        "usr": "Reply with exactly: hello",
    },
    timeout=10,
)
created.raise_for_status()
response_id = created.json()["id"]

while True:
    polled = requests.get(
        f"{BASE_URL}/v1/responses/{response_id}",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=10,
    )
    polled.raise_for_status()
    data = polled.json()
    if data["status"] == "completed":
        print(data["output_text"])
        break
    if data["status"] in ("failed", "cancelled"):
        raise RuntimeError(data)
    time.sleep(2)
```

현재 서버는 비동기 queue 방식이므로 요청 생성 응답은 최종 GPT 답변이 아닙니다. `id`로 `GET /v1/responses/{id}`를 직접 poll해야 합니다.

### Chat Completions

`/v1/chat/completions`도 queue에 들어갑니다. 생성 응답은 완료 결과가 아니라 job 객체이며, 결과는 `/v1/jobs/{job_id}`로 조회합니다.

```bash
CREATE=$(curl -sS http://192.168.0.16:31835/v1/chat/completions \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4-mini","messages":[{"role":"user","content":"hello"}]}')

JOB_ID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$CREATE")

curl -sS "http://192.168.0.16:31835/v1/jobs/${JOB_ID}" \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" | python3 -m json.tool
```

## Deployment

dogok에서:

```bash
cd /workspace/thhwang/external-gpt-oauth
sudo -n docker compose up -d --build
sudo -n docker compose ps
```

현재 Compose는 dogok LAN IP에만 바인딩합니다.

```yaml
ports:
  - "192.168.0.16:31835:8000"
```

같은 공유기/LAN 내부 기기에서 `http://192.168.0.16:31835`로 접근할 수 있습니다. 인터넷에 공개할 때는 router port forwarding만으로는 TLS가 없으므로, 가능하면 HTTPS reverse proxy나 터널 뒤에 두고 API key를 긴 랜덤 값으로 유지합니다.

## OAuth Login

ChatGPT/Codex OAuth 계정 연결은 dogok에서 수행합니다.

```bash
cd /workspace/thhwang/external-gpt-oauth
CODEX_HOME=/workspace/thhwang/external-gpt-oauth/.codex-auth npx @openai/codex login
sudo -n docker compose restart
```

로그인 후 `/health`에서 아래 값이 모두 `true`여야 합니다.

```json
{
  "oauth_proxy": {
    "healthy": true,
    "auth_file_exists": true
  }
}
```

서버는 auth file을 감시하며, access token 만료 7일 전부터 refresh token으로 자동 갱신합니다. 갱신 후에는 내부 `openai-oauth` 프록시를 재시작합니다.

## Project Layout

```text
app/
  main.py          # FastAPI routes and lifespan
  db.py            # SQLite queue storage
  queue_worker.py  # background workers
  oauth_proxy.py   # openai-oauth subprocess manager
  auth_refresh.py  # OAuth token refresh loop
  upstream.py      # local openai-oauth HTTP client
docs/
  OPERATIONS.md    # deployment, testing, troubleshooting
```

## More Docs

- [Operations](docs/OPERATIONS.md)
