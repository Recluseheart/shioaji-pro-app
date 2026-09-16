#!/usr/bin/env bash
# Run Gateway PostgreSQL integration against a disposable, loopback-only DB.
set -euo pipefail

container_name="autotrade-gateway-pg-it-$$"
database="autotrade_test"
username="autotrade_test"
password="autotrade_test_local_only"
restore_database="autotrade_restore"

cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm -d --name "$container_name" \
  -e "POSTGRES_USER=$username" \
  -e "POSTGRES_PASSWORD=$password" \
  -e "POSTGRES_DB=$database" \
  -p 127.0.0.1::5432 postgres:16-alpine >/dev/null

restore_drill() {
  local schema="gateway_restore_$$_${RANDOM}"
  local archive="/tmp/${schema}.dump"
  local dsn="postgresql://$username:$password@127.0.0.1:$port/$database"

  # Use the real schema migrations, then dump only this fresh isolated schema.
  AUTOTRADE_GATEWAY_DATABASE_URL="$dsn" \
    AUTOTRADE_GATEWAY_PG_SCHEMA="$schema" \
    uv run python -c 'from services.autotrade_gateway.postgres_intent_store import PostgresIntentStore; import os; store = PostgresIntentStore(os.environ["AUTOTRADE_GATEWAY_DATABASE_URL"], os.environ["AUTOTRADE_GATEWAY_PG_SCHEMA"]); store.close()'
  docker exec "$container_name" pg_dump -U "$username" -d "$database" --schema="$schema" --format=custom --file="$archive"
  docker exec "$container_name" pg_restore --list "$archive" >/dev/null
  docker exec "$container_name" createdb -U "$username" "$restore_database"
  docker exec "$container_name" pg_restore -U "$username" -d "$restore_database" "$archive"
  docker exec "$container_name" psql -U "$username" -d "$restore_database" -v ON_ERROR_STOP=1 -Atqc \
    "SELECT count(*) FROM \"$schema\".gateway_schema_migrations WHERE version = 3" | grep -qx '1'
}

for _ in {1..30}; do
  if docker exec "$container_name" pg_isready -U "$username" -d "$database" >/dev/null 2>&1; then
    port="$(docker port "$container_name" 5432 | sed 's/.*://')"
    AUTOTRADE_GATEWAY_TEST_POSTGRES_DSN="postgresql://$username:$password@127.0.0.1:$port/$database" \
      uv run python -m unittest -v services.autotrade_gateway.test_postgres_intent_store
    restore_drill
    printf '%s\n' 'PostgreSQL disposable backup/restore drill: OK'
    exit 0
  fi
  sleep 1
done

echo "disposable PostgreSQL did not become ready" >&2
exit 1
