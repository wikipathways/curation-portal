#!/bin/sh
# Container entrypoint: hydrate Docker secrets, run DB migrations, then start the app.
set -e

# Swarm mounts secrets at /run/secrets/<name>. Overridable only so the tests can point it elsewhere.
SECRETS_DIR="${SECRETS_DIR:-/run/secrets}"

# Load a secret file into an env var, unless the env var is already set. Keeps sensitive values
# out of the image and out of `docker inspect`.
_load_secret() {
  eval "current=\$$1"
  if [ -z "$current" ] && [ -f "$2" ]; then
    export "$1=$(cat "$2")"
  fi
}

# Every secret is read under both names: the portal_* one new deployments should use, and the
# wpsubmit_* one the service was first deployed with. The app reads PORTAL_* first and falls back
# to WPSUBMIT_* (app/config.py), so a deployment can rename its secrets whenever it likes.
for name in SESSION_SECRET GITHUB_OAUTH_CLIENT_SECRET GITHUB_WEBHOOK_SECRET \
            TOKEN_ENCRYPTION_KEY DATABASE_URL; do
  case "$name" in
    GITHUB_OAUTH_CLIENT_SECRET) file=oauth_client_secret ;;
    GITHUB_WEBHOOK_SECRET)      file=webhook_secret ;;
    *) file=$(echo "$name" | tr 'A-Z' 'a-z') ;;
  esac
  _load_secret "PORTAL_$name"   "$SECRETS_DIR/portal_$file"
  _load_secret "WPSUBMIT_$name" "$SECRETS_DIR/wpsubmit_$file"
done
# The GitHub App private key stays a file path:
#   PORTAL_GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/portal_app_key   (or the WPSUBMIT_ names)

# Apply migrations before serving, but only for a real (non-SQLite) database — SQLite dev uses
# create_all. Idempotent: `upgrade head` on an up-to-date DB is a no-op. The URL is resolved the
# way the app resolves it, new name first; checking only the old one would skip migrations on a
# deployment that had renamed, and the app would then start against an old schema.
case "${PORTAL_DATABASE_URL:-${WPSUBMIT_DATABASE_URL:-}}" in
  postgresql*|postgres*)
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
    ;;
esac

exec "$@"
