#!/usr/bin/env bash
# Run Gateway PostgreSQL integration against a disposable, loopback-only DB.
set -euo pipefail

container_name="autotrade-gateway-pg-it-$$"
database="autotrade_test"
username="autotrade_test"
password="autotrade_test_local_only"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm -d --name "$container_name" \
  -e "POSTGRES_USER=$username" \
  -e "POSTGRES_PASSWORD=$password" \
  -e "POSTGRES_DB=$database" \
  -p 127.0.0.1::5432 postgres:16-alpine >/dev/null

for _ in {1..30}; do
  if docker exec "$container_name" pg_isready -U "$username" -d "$database" >/dev/null 2>&1; then
    port="$(docker port "$container_name" 5432 | sed 's/.*://')"
    AUTOTRADE_GATEWAY_TEST_POSTGRES_DSN="postgresql://$username:$password@127.0.0.1:$port/$database" \
      uv run python -m unittest -v services.autotrade_gateway.test_postgres_intent_store
    exit 0
  fi
  sleep 1
done

echo "disposable PostgreSQL did not become ready" >&2
exit 1
