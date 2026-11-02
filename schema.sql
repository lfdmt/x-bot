-- Rode uma vez no SQL Editor do Supabase.

create table if not exists fontes (
  id           bigint generated always as identity primary key,
  url          text not null unique,
  titulo       text not null,
  resumo       text,
  idioma       text not null check (idioma in ('pt', 'es')),
  publicado_em timestamptz,
  coletado_em  timestamptz not null default now(),
  usado        boolean not null default false
);

create table if not exists rascunhos (
  id                  bigint generated always as identity primary key,
  fonte_id            bigint references fontes(id) on delete set null,
  texto               text not null,
  link                text not null,
  status              text not null default 'pendente'
                      check (status in ('pendente', 'aprovado', 'descartado', 'publicado', 'erro')),
  telegram_message_id bigint,
  tweet_id            text,
  resposta_id         text,
  erro                text,
  criado_em           timestamptz not null default now(),
  publicado_em        timestamptz
);

create index if not exists rascunhos_status_idx on rascunhos (status, criado_em);
create index if not exists fontes_usado_idx on fontes (usado, publicado_em desc);

-- Guarda o offset do getUpdates do Telegram entre execuções.
create table if not exists bot_estado (
  chave text primary key,
  valor text not null
);

-- O bot usa a service key (ignora RLS). Ativar RLS sem políticas bloqueia a chave anon.
alter table fontes     enable row level security;
alter table rascunhos  enable row level security;
alter table bot_estado enable row level security;
