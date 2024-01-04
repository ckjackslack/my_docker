#!/bin/bash
set -e

# Example: Create a role and grant permissions
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER myuser WITH PASSWORD 'mypassword';
    GRANT ALL PRIVILEGES ON DATABASE "$POSTGRES_DB" TO myuser;
EOSQL

# chmod +x init_script.sh