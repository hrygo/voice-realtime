\set ON_ERROR_STOP on

-- 本机 PostgreSQL 管理员一次性执行；应用运行时不会创建角色或 schema。
DO $bootstrap$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sona_app') THEN
        CREATE ROLE sona_app LOGIN;
    END IF;
END
$bootstrap$;

DO $bootstrap$
DECLARE
    existing_owner text;
BEGIN
    SELECT pg_get_userbyid(nspowner)
    INTO existing_owner
    FROM pg_namespace
    WHERE nspname = 'sona';

    IF existing_owner IS NULL THEN
        CREATE SCHEMA sona AUTHORIZATION sona_app;
    ELSIF existing_owner <> 'sona_app' THEN
        RAISE EXCEPTION
            'schema sona owner is %, expected sona_app',
            existing_owner;
    END IF;
END
$bootstrap$;

REVOKE ALL ON SCHEMA sona FROM PUBLIC;
GRANT CONNECT ON DATABASE knowledge TO sona_app;
GRANT USAGE, CREATE ON SCHEMA sona TO sona_app;
