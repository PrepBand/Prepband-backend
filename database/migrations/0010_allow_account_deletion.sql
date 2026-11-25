-- Phase 6: let account deletion work (privacy policy: "we'll delete your
-- account and everything linked to it"; spec section 9 deletion policy).
--
-- attempts_immutable_once_terminal also fires on DELETE, so deleting a user
-- who had ever submitted an attempt failed: the ON DELETE CASCADE from
-- auth.users reached a submitted attempt and the trigger raised. Earlier
-- test-account cleanups had to switch the trigger off by hand.
--
-- New rule: a terminal attempt may be deleted only as part of deleting its
-- owner's account, i.e. when the auth.users row is already gone (the
-- cascade runs after the user row is deleted). Every other delete or update
-- of a terminal attempt is still refused. Students have no delete policy on
-- attempts at all, so this opens nothing to the API or the browser.

create or replace function public.prevent_terminal_attempt_mutation()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if tg_op = 'DELETE'
     and not exists (select 1 from auth.users where id = old.student_id) then
    return old; -- the owner's account is being deleted
  end if;
  if old.status != 'in_progress' then
    raise exception 'attempt % is % and no longer editable', old.id, old.status;
  end if;
  return coalesce(new, old);
end;
$$;

revoke execute on function public.prevent_terminal_attempt_mutation() from public, anon, authenticated;
