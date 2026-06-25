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

## Health Checks

dogok에서:

```bash
curl http://192.168.0.16:31835/health \
  -H "Authorization: Bearer classday-api" | python3 -m json.tool
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

## End-to-End Test

로컬 또는 같은 LAN의 클라이언트에서:

```bash
CREATE=$(curl -sS http://192.168.0.16:31835/v1/responses \
  -H "Authorization: Bearer classday-api" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"gpt-5.4-mini",
    "system":"You are a concise assistant.",
    "usr":"Reply with exactly: lan-ok"
  }')

RID=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<< "$CREATE")

for i in $(seq 1 30); do
  OUT=$(curl -sS "http://192.168.0.16:31835/v1/responses/${RID}" \
    -H "Authorization: Bearer classday-api")
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
- `GPT_QUEUE_CONCURRENCY` 기본값은 `2`입니다.
- `GPT_JOB_TIMEOUT_SECONDS` 기본값은 `900`입니다.
- `GPT_JOB_MAX_ATTEMPTS` 기본값은 `3`입니다.

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
  -H "Authorization: Bearer classday-api"
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
