-- Migração 002: mídia (imagem, gráfico, GIF, cards com logos) e novas fontes (feeds em inglês, mercado, posts do X).
-- Rode uma vez no SQL Editor do Supabase.

alter table fontes drop constraint if exists fontes_idioma_check;
alter table fontes add constraint fontes_idioma_check check (idioma in ('pt', 'es', 'en'));

alter table fontes
  add column if not exists tipo       text not null default 'noticia',
  add column if not exists origem     text,
  add column if not exists imagem_url text,
  add column if not exists dados      jsonb;

alter table fontes drop constraint if exists fontes_tipo_check;
alter table fontes add constraint fontes_tipo_check check (tipo in ('noticia', 'mercado', 'x'));

-- Até 4 imagens por tweet (limite do X).
alter table rascunhos
  add column if not exists midia_tipo text,
  add column if not exists midia_urls text[];

alter table rascunhos drop constraint if exists rascunhos_midia_tipo_check;
alter table rascunhos add constraint rascunhos_midia_tipo_check
  check (midia_tipo is null or midia_tipo in ('materia', 'grafico', 'gif', 'logos'));
