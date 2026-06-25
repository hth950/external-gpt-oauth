# Operations

운영 기준 경로:

```text
/workspace/thhwang/external-gpt-oauth
```

## Service Commands

```bash
cd /workspace/thhwang/external-gpt-oauth

sudo -n docker compose ps
sudo -n docker compose logs --tail=100 external-gpt-oauth
sudo -n docker compose restart
sudo -n docker compose up -d --build
```

## Runtime Files

이 파일들은 git에 커밋하지 않습니다.

```text
.env                    # DOGOK_PROXY_API_KEY and runtime settings
.codex-auth/auth.json   # Codex/ChatGPT OAuth token file
data/jobs.sqlite3       # durable job queue
logs/                   # reserved for logs
```

현재 운영 API key:

```bash
grep '^DOGOK_PROXY_API_KEY=' /workspace/thhwang/external-gpt-oauth/.env
```

예제 명령을 실행하는 쉘에서는 실제 key를 환경변수로 지정합니다.

```bash
export DOGOK_PROXY_API_KEY="$(grep '^DOGOK_PROXY_API_KEY=' /workspace/thhwang/external-gpt-oauth/.env | cut -d= -f2-)"
```

## Health Checks

dogok에서:

```bash
curl http://192.168.0.16:31835/health \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" | python3 -m json.tool
```

정상 기준:

```json
{
  "status": "ok",
  "oauth_proxy": {
    "healthy": true,
    "auth_file_exists": true
  }
}
```

포트 바인딩 확인:

```bash
ss -ltnp | grep 31835
```

정상 예:

```text
LISTEN ... 192.168.0.16:31835 ...
```

## Swagger UI

Swagger/OpenAPI는 외부 노출을 고려해 기본 비활성화합니다.

임시 활성화:

```bash
cd /workspace/thhwang/external-gpt-oauth
python3 - <<'PY'
from pathlib import Path
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
out = []
found = False
for line in lines:
    if line.startswith("DOGOK_PROXY_ENABLE_DOCS="):
        out.append("DOGOK_PROXY_ENABLE_DOCS=true")
        found = True
    else:
        out.append(line)
if not found:
    out.append("DOGOK_PROXY_ENABLE_DOCS=true")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
sudo -n docker compose restart
```

Swagger URL:

```text
http://192.168.0.16:31835/docs
```

사용 방법:

1. 우측 상단 `Authorize`를 누릅니다.
2. token 값으로 실제 `DOGOK_PROXY_API_KEY` 값을 입력합니다.
3. `/v1/responses`를 열고 `Try it out`을 누릅니다.
4. `system_usr` 예시를 선택하거나 아래 형태로 body를 넣습니다.

```json
{
  "model": "gpt-5.4-mini",
  "system": "You are a concise assistant.",
  "usr": "Reply with exactly: hello",
  "reasoning_effort": "low"
}
```

요청 생성 응답의 `id`를 복사한 뒤 `/v1/responses/{response_id}`에서 poll합니다.

확인이 끝나면 다시 비활성화합니다.

```bash
cd /workspace/thhwang/external-gpt-oauth
python3 - <<'PY'
from pathlib import Path
path = Path(".env")
lines = path.read_text(encoding="utf-8").splitlines()
out = []
found = False
for line in lines:
    if line.startswith("DOGOK_PROXY_ENABLE_DOCS="):
        out.append("DOGOK_PROXY_ENABLE_DOCS=false")
        found = True
    else:
        out.append(line)
if not found:
    out.append("DOGOK_PROXY_ENABLE_DOCS=false")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
sudo -n docker compose restart
```

## End-to-End Test

로컬 또는 같은 LAN의 클라이언트에서:

```bash
CREATE=$(curl -sS http://192.168.0.16:31835/v1/responses \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"gpt-5.4-mini",
    "system":"You are a concise assistant.",
    "usr":"Reply with exactly: lan-ok",
    "reasoning_effort":"low"
  }')

RID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$CREATE")

for i in $(seq 1 30); do
  OUT=$(curl -sS "http://192.168.0.16:31835/v1/responses/${RID}" \
    -H "Authorization: Bearer $DOGOK_PROXY_API_KEY")
  STATUS=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status"))' <<< "$OUT")
  echo "poll_${i}=${STATUS}"
  if [ "$STATUS" != queued ] && [ "$STATUS" != in_progress ]; then
    echo "$OUT" | python3 -m json.tool
    break
  fi
  sleep 2
done
```

정상 완료 시 `status=completed`와 `output_text=lan-ok`가 나옵니다.

## Queue Behavior

- 생성 요청은 SQLite에 저장된 뒤 즉시 `202 Accepted`로 job id를 반환합니다.
- worker가 `queued` job을 `in_progress`로 claim하고 내부 GPT 호출을 수행합니다.
- 완료 결과는 `completed` 상태로 저장됩니다.
- 서버 재시작 중이던 `in_progress` job은 startup 시 재시도 가능 상태로 복구됩니다.
- `GPT_QUEUE_CONCURRENCY` 기본값은 `2`입니다. 현재 dogok 운영값은 `5`입니다.
- `GPT_JOB_TIMEOUT_SECONDS` 기본값은 `900`입니다.
- `GPT_JOB_MAX_ATTEMPTS` 기본값은 `3`입니다.
- `GPT_DEFAULT_REASONING_EFFORT` 기본값은 `low`입니다.

현재 동시성 확인:

```bash
curl http://192.168.0.16:31835/health \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY" | python3 -m json.tool
```

응답의 `settings.queue_concurrency` 값이 실제 worker 동시성입니다.

```json
{
  "settings": {
    "queue_concurrency": 5,
    "default_reasoning_effort": "low"
  }
}
```

동시성 변경:

```bash
cd /workspace/thhwang/external-gpt-oauth
vi .env
```

```env
GPT_QUEUE_CONCURRENCY=4
```

적용:

```bash
sudo -n docker compose up -d --force-recreate
```

주의: ChatGPT/Codex OAuth 계정 하나로 너무 많은 동시 요청을 보내면 upstream 제한, 지연, 실패가 늘 수 있습니다. dogok은 현재 `5`로 운영하며, 실패가 늘면 `3` 정도로 낮춰 봅니다.

reasoning effort 변경:

```env
GPT_DEFAULT_REASONING_EFFORT=low
```

요청별 override:

```json
{
  "model": "gpt-5.5",
  "system": "You are concise.",
  "usr": "Reply with exactly: ok",
  "reasoning_effort": "medium"
}
```

Responses 스타일도 지원합니다.

```json
{
  "model": "gpt-5.5",
  "input": "Reply with exactly: ok",
  "reasoning": {"effort": "medium"}
}
```

지원 가능한 값은 모델별로 다를 수 있지만, 서버 schema는 `none`, `minimal`, `low`, `medium`, `high`, `xhigh`를 허용합니다.

## OAuth Refresh

`app/auth_refresh.py`가 `.codex-auth/auth.json`의 access token expiry를 확인합니다.

기본값:

```text
GPT_AUTH_REFRESH_MARGIN_SECONDS=604800
GPT_AUTH_REFRESH_LOOP_INTERVAL_SECONDS=3600
```

즉, 만료 7일 전부터 1시간마다 refresh를 시도합니다. refresh 성공 시 auth file을 원자적으로 교체하고 `openai-oauth` subprocess를 재시작합니다.

수동 재로그인이 필요할 때:

```bash
cd /workspace/thhwang/external-gpt-oauth
CODEX_HOME=/workspace/thhwang/external-gpt-oauth/.codex-auth npx @openai/codex login
sudo -n docker compose restart
```

## Troubleshooting

### `401 Unauthorized`

`Authorization` header가 없거나 API key가 틀린 상태입니다.

```bash
curl http://192.168.0.16:31835/health \
  -H "Authorization: Bearer $DOGOK_PROXY_API_KEY"
```

### `oauth_not_configured`

`.codex-auth/auth.json`이 없습니다. dogok에서 OAuth login을 다시 수행합니다.

### `oauth_proxy_unavailable`

auth file은 있지만 내부 `openai-oauth` subprocess가 healthy가 아닙니다.

```bash
cd /workspace/thhwang/external-gpt-oauth
sudo -n docker compose logs --tail=100 external-gpt-oauth
sudo -n docker compose restart
```

### LAN에서 접속 불가

1. dogok IP 확인:

   ```bash
   hostname -I
   ```

2. compose binding 확인:

   ```bash
   sudo -n docker compose ps
   ss -ltnp | grep 31835
   ```

3. 같은 공유기/LAN인지 확인합니다. 현재 compose는 `192.168.0.16:31835`에만 바인딩합니다.
