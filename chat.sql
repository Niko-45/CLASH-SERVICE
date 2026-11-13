-- Supabase -> SQL Editor -> ин кодро ичро кунед (як маротиба)
create table messages (
  id bigserial primary key,
  order_id bigint references orders(id) on delete cascade,
  sender_id bigint not null,
  text text not null,
  created_at timestamptz default now()
);
create index on messages (order_id, id);
alter table messages enable row level security;
grant all on table messages to service_role;
grant usage, select on sequence messages_id_seq to service_role;
