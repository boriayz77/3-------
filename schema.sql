create table if not exists users (
    id bigint primary key,
    username text,
    full_name text,
    current_room text,
    current_repair_type text,
    created_at timestamptz default now(),
    updated_at timestamptz default now()
);

create table if not exists requests (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    room text,
    repair_type text,
    topic text,
    materials text,
    summary text,
    created_at timestamptz default now()
);

create table if not exists history (
    id bigserial primary key,
    user_id bigint references users(id) on delete cascade,
    request_id bigint references requests(id) on delete set null,
    user_message text not null,
    bot_answer text not null,
    created_at timestamptz default now()
);

