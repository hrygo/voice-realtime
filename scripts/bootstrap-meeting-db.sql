\set ON_ERROR_STOP on

-- 本机 PostgreSQL 管理员一次性执行；应用运行时不会创建角色或 schema。
DO $bootstrap$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'voice_realtime_app') THEN
        CREATE ROLE voice_realtime_app LOGIN;
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
    WHERE nspname = 'voice_realtime';

    IF existing_owner IS NULL THEN
        CREATE SCHEMA voice_realtime AUTHORIZATION voice_realtime_app;
    ELSIF existing_owner <> 'voice_realtime_app' THEN
        RAISE EXCEPTION
            'schema voice_realtime owner is %, expected voice_realtime_app',
            existing_owner;
    END IF;
END
$bootstrap$;

REVOKE ALL ON SCHEMA voice_realtime FROM PUBLIC;
GRANT CONNECT ON DATABASE knowledge TO voice_realtime_app;
GRANT USAGE, CREATE ON SCHEMA voice_realtime TO voice_realtime_app;
