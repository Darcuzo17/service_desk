-- =============================================================
-- SERVICE DESK — SCHEMA INIT
-- Запустить от имени service_desk_user в базе service_desk_db:
--   psql -U service_desk_user -d service_desk_db -f sql/01_schema.sql
-- =============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE SCHEMA IF NOT EXISTS sm;

-- -------------------------------------------------------------
-- sm.users
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.users (
    user_uid        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_name       varchar(12)  NOT NULL,
    first_name      varchar(100) NOT NULL,
    last_name       varchar(100) NOT NULL,
    middel_name     varchar(100) NULL,
    email           varchar(255) NOT NULL,
    mobile          varchar(20)  NULL,
    work_phone      varchar(20)  NULL,
    gender          bpchar(1)    NULL,
    title           varchar(255) NULL,
    department      varchar(255) NULL,
    company         varchar(255) NULL,
    manager_uid     uuid NULL,
    work_status     varchar(20)  NULL,
    is_vip          bool         DEFAULT false NULL,
    is_deactivated  bool         DEFAULT false NULL,
    is_temp_deactivated bool     DEFAULT false NULL,
    last_loggon_date    timestamp NULL,
    password_expires    timestamp NULL,
    create_date     timestamp    DEFAULT CURRENT_TIMESTAMP NULL,
    update_date     timestamp    DEFAULT CURRENT_TIMESTAMP NULL,
    create_by       uuid         NOT NULL,
    update_by       uuid         NULL,
    CONSTRAINT users_email_key    UNIQUE (email),
    CONSTRAINT users_user_name_key UNIQUE (user_name),
    CONSTRAINT users_gender_check CHECK (gender = ANY (ARRAY['M'::bpchar, 'F'::bpchar, 'O'::bpchar]))
);

-- -------------------------------------------------------------
-- sm.passwords
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.passwords (
    user_uid            uuid  NOT NULL REFERENCES sm.users(user_uid) ON DELETE CASCADE,
    passwordhash        text  NULL,
    is_first_login      bool  DEFAULT true,
    must_change_password bool DEFAULT false,
    failed_attempts     int   DEFAULT 0,
    CONSTRAINT passwords_pkey PRIMARY KEY (user_uid)
);

-- -------------------------------------------------------------
-- sm.user_roles  (app-level, outside original schema)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.user_roles (
    user_uid uuid NOT NULL REFERENCES sm.users(user_uid) ON DELETE CASCADE,
    role     varchar(32) NOT NULL DEFAULT 'user',
    CONSTRAINT user_roles_pkey PRIMARY KEY (user_uid)
);

-- -------------------------------------------------------------
-- sm.work_groups
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.work_groups (
    work_group_uid    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    group_name        varchar(100) NOT NULL,
    isactive          bool DEFAULT true NULL,
    group_description text NULL,
    group_owner_uid   uuid NULL,
    create_date       timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    update_date       timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    create_by         uuid NOT NULL,
    update_by         uuid NULL
);

-- -------------------------------------------------------------
-- sm.user_work_groups
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.user_work_groups (
    user_uid       uuid NOT NULL REFERENCES sm.users(user_uid)            ON DELETE CASCADE,
    work_group_uid uuid NOT NULL REFERENCES sm.work_groups(work_group_uid) ON DELETE CASCADE,
    assigned_date  timestamptz DEFAULT CURRENT_TIMESTAMP NULL,
    is_primary     bool DEFAULT false NULL,
    CONSTRAINT user_work_groups_pkey PRIMARY KEY (user_uid, work_group_uid)
);

-- -------------------------------------------------------------
-- sm.sla_policies
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.sla_policies (
    sla_uid               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_name           varchar(100) NOT NULL,
    description           text NULL,
    response_time_hours   int4 NOT NULL DEFAULT 8,
    resolution_time_hours int4 NOT NULL DEFAULT 24,
    is_active             bool DEFAULT true NULL,
    create_date           timestamptz DEFAULT CURRENT_TIMESTAMP NULL,
    create_by             uuid NOT NULL
);

-- -------------------------------------------------------------
-- sm.service_catalog
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.service_catalog (
    catalog_uid         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    catalog_name        varchar(200) NOT NULL,
    catalog_path        text NOT NULL,
    parent_uid          uuid NULL REFERENCES sm.service_catalog(catalog_uid),
    catalog_type        varchar(50) NOT NULL DEFAULT 'category',
    work_group_uid      uuid NULL REFERENCES sm.work_groups(work_group_uid),
    ticket_type         varchar(100) DEFAULT 'service_request',
    priority            varchar(20)  DEFAULT 'medium',
    approval_required   bool DEFAULT false NULL,
    is_active           bool DEFAULT true NULL,
    sla_uid             uuid NULL REFERENCES sm.sla_policies(sla_uid),
    catalog_description text NULL,
    create_date         timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    update_date         timestamp DEFAULT CURRENT_TIMESTAMP NULL,
    create_by           uuid NOT NULL,
    update_by           uuid NULL
);

-- -------------------------------------------------------------
-- sm.tickets
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.tickets (
    ticket_uid    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_number varchar(50)  NOT NULL,
    catalog_uid   uuid         NOT NULL REFERENCES sm.service_catalog(catalog_uid),
    summary       varchar(500) NOT NULL,
    description   text         NOT NULL,
    requester_uid uuid         NOT NULL REFERENCES sm.users(user_uid),
    recipient_uid uuid         NOT NULL REFERENCES sm.users(user_uid),
    performer_uid uuid         NULL     REFERENCES sm.users(user_uid),
    status        varchar(50)  NOT NULL DEFAULT 'new',
    priority      varchar(20)  NULL     DEFAULT 'medium',
    deadline_at   timestamptz  NULL,
    resolved_at   timestamptz  NULL,
    closed_at     timestamptz  NULL,
    created_at    timestamptz  DEFAULT CURRENT_TIMESTAMP NULL,
    updated_at    timestamptz  DEFAULT CURRENT_TIMESTAMP NULL,
    created_by    uuid         NOT NULL REFERENCES sm.users(user_uid),
    updated_by    uuid         NULL     REFERENCES sm.users(user_uid),
    CONSTRAINT tickets_ticket_number_key UNIQUE (ticket_number)
);

-- -------------------------------------------------------------
-- sm.ticket_history
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.ticket_history (
    history_uid  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_uid   uuid NOT NULL REFERENCES sm.tickets(ticket_uid) ON DELETE CASCADE,
    field_name   varchar(100) NOT NULL,
    old_value    text NULL,
    new_value    text NULL,
    changed_by   uuid NOT NULL REFERENCES sm.users(user_uid),
    changed_date timestamptz DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- sm.ticket_param_values
-- (комментарии, внутренние заметки, решения по согласованию)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.ticket_param_values (
    param_value_uid uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_uid      uuid         NOT NULL REFERENCES sm.tickets(ticket_uid) ON DELETE CASCADE,
    param_name      varchar(100) NOT NULL,
    param_value     text         NULL,
    param_type      varchar(50)  NULL,
    author_uid      uuid         NULL REFERENCES sm.users(user_uid),
    create_date     timestamptz  DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- sm.approval_routes
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.approval_routes (
    route_uid    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    route_name   varchar(200) NOT NULL,
    catalog_uid  uuid NOT NULL REFERENCES sm.service_catalog(catalog_uid) ON DELETE CASCADE,
    is_active    bool DEFAULT true NULL,
    create_date  timestamptz DEFAULT CURRENT_TIMESTAMP NULL,
    create_by    uuid NOT NULL REFERENCES sm.users(user_uid)
);

-- -------------------------------------------------------------
-- sm.approval_steps
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.approval_steps (
    step_uid       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    route_uid      uuid NOT NULL REFERENCES sm.approval_routes(route_uid) ON DELETE CASCADE,
    step_order     int4 NOT NULL DEFAULT 1,
    step_name      varchar(200) NULL,
    approver_uid   uuid NULL REFERENCES sm.users(user_uid),
    approver_role  varchar(32) NULL
);

-- -------------------------------------------------------------
-- sm.ticket_approvals
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.ticket_approvals (
    approval_uid  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_uid    uuid NOT NULL REFERENCES sm.tickets(ticket_uid) ON DELETE CASCADE,
    step_order    int4 NOT NULL DEFAULT 1,
    step_name     varchar(200) NULL,
    approver_uid  uuid NULL REFERENCES sm.users(user_uid),
    status        varchar(20) NOT NULL DEFAULT 'pending',
    comment       text NULL,
    decided_at    timestamptz NULL,
    create_date   timestamptz DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- sm.notifications
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.notifications (
    notification_uid  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_uid          uuid NOT NULL REFERENCES sm.users(user_uid) ON DELETE CASCADE,
    message           text NOT NULL,
    ticket_uid        uuid NULL REFERENCES sm.tickets(ticket_uid) ON DELETE SET NULL,
    is_read           bool DEFAULT false NULL,
    create_date       timestamptz DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- sm.ticket_templates
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.ticket_templates (
    template_uid    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    template_name   varchar(200) NOT NULL,
    catalog_uid     uuid NOT NULL REFERENCES sm.service_catalog(catalog_uid) ON DELETE CASCADE,
    summary         varchar(500) NOT NULL,
    description     text NOT NULL,
    priority        varchar(20) NULL,
    created_by      uuid NOT NULL REFERENCES sm.users(user_uid),
    is_public       bool DEFAULT false NULL,
    create_date     timestamptz DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- sm.audit_log
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sm.audit_log (
    audit_uid      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_uid       uuid NULL REFERENCES sm.users(user_uid) ON DELETE SET NULL,
    action         varchar(64) NOT NULL,
    entity_type    varchar(64) NULL,
    entity_uid     uuid NULL,
    details        text NULL,
    ip_address     varchar(64) NULL,
    create_date    timestamptz DEFAULT CURRENT_TIMESTAMP NULL
);

-- -------------------------------------------------------------
-- Индексы для производительности
-- -------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_tickets_status        ON sm.tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_requester     ON sm.tickets(requester_uid);
CREATE INDEX IF NOT EXISTS idx_tickets_performer     ON sm.tickets(performer_uid);
CREATE INDEX IF NOT EXISTS idx_tickets_catalog       ON sm.tickets(catalog_uid);
CREATE INDEX IF NOT EXISTS idx_tickets_created_at    ON sm.tickets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket ON sm.ticket_history(ticket_uid);
CREATE INDEX IF NOT EXISTS idx_ticket_params_ticket  ON sm.ticket_param_values(ticket_uid);
CREATE INDEX IF NOT EXISTS idx_ticket_params_type    ON sm.ticket_param_values(param_type);
CREATE INDEX IF NOT EXISTS idx_uwg_user              ON sm.user_work_groups(user_uid);
CREATE INDEX IF NOT EXISTS idx_uwg_group             ON sm.user_work_groups(work_group_uid);

-- Права
GRANT USAGE  ON SCHEMA sm TO service_desk_user;
GRANT ALL    ON ALL TABLES    IN SCHEMA sm TO service_desk_user;
GRANT ALL    ON ALL SEQUENCES IN SCHEMA sm TO service_desk_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA sm TO service_desk_user;
