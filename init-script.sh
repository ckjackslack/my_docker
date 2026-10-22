#!/usr/bin/env bash
# Postgres first-start hook, mounted into /docker-entrypoint-initdb.d by
# docker-compose.local.yml. It only runs when the data volume is empty.
#
# Creates the application role DATABASE_USER (password DATABASE_PASSWORD) unless it is the
# superuser itself, and makes it the owner of POSTGRES_DB. Values are passed to psql as
# variables and quoted with format(%I / %L), so they are never spliced into SQL by the shell.
set -eo pipefail

init_app_role() {
    local superuser="${POSTGRES_USER:?POSTGRES_USER must be set}"
    local database="${POSTGRES_DB:-$superuser}"
    local app_user="${DATABASE_USER:-}"
    local app_password="${DATABASE_PASSWORD:-}"

    if [[ -z "$app_user" || "$app_user" == "$superuser" ]]; then
        echo "init-script: no separate application role requested, nothing to do"
        return 0
    fi
    if [[ -z "$app_password" ]]; then
        echo "init-script: DATABASE_PASSWORD must be set for role '$app_user'" >&2
        return 1
    fi

    psql -v ON_ERROR_STOP=1 \
        --username "$superuser" \
        --dbname "$database" \
        --set app_user="$app_user" \
        --set app_password="$app_password" \
        --set app_db="$database" <<'EOSQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user')\gexec
SELECT format('ALTER DATABASE %I OWNER TO %I', :'app_db', :'app_user')\gexec
SELECT format('GRANT ALL ON SCHEMA public TO %I', :'app_user')\gexec
EOSQL
    echo "init-script: role '$app_user' owns database '$database'"
}

init_app_role
