# Multi-user extension / 扩展：多用户

v1 of this fork is a single-user app on the machine that cloned the
repo. `scripts/meeting-dev.sh` and `scripts/meeting-mock-demo.sh` keep
working with no account, no tenant, and no extra host. Full multi-user
(issue #4) stays deferred. This page is the seam a later contributor
can implement locally.

本 fork 的 v1 是克隆仓库的那台机器上的单用户应用。
`scripts/meeting-dev.sh` 和 `scripts/meeting-mock-demo.sh` 不需要账号、
租户或另一台主机。完整多用户（issue #4）继续延期。这篇说明的是以后在
本机接着做时要接的缝。

## Current scope / 当前范围

- One process, one SQLite file (`MEETING_DB_PATH`, default
  `backend/data/meeting.db`).
- Every run is stored with `user_id` `local`.
- `SingleUserIdentity` is the only identity provider.
- `MEETING_USER_ID` and the `X-Meeting-User` request header are
  reserved. v1 reads them and drops them. Setting either value leaves
  the store, the review token, and the Feishu login unchanged.
- `scripts/meeting-dev.sh --check` does not read `MEETING_USER_ID`.
  A missing value stays the local user, so the default mock path
  still passes the check.
- `/meeting/ui` does not read the header. Run routes under
  `/meeting/runs` accept it and still resolve `local`.

Persistence stays on that one local file. Cloud hosting is outside
this extension.

持久化仍是这一个本地文件。云主机不在这条扩展里。

## Where the seam lives / 扩展点

| Piece | File | Symbol |
| --- | --- | --- |
| Caller | `backend/app/meeting/identity.py` | `UserContext`, `IdentityProvider`, `SingleUserIdentity`, `build_identity` |
| Reserved names | same file | `LOCAL_USER_ID` (`local`), `MEETING_USER_ENV` (`MEETING_USER_ID`), `MEETING_USER_HEADER` (`X-Meeting-User`) |
| Scope hook | same file | `ignore_user_scope` |
| HTTP | `backend/app/meeting/router.py` | `resolve_request_user` passes the header into `IdentityProvider.current` |
| Runs | `backend/app/meeting/service.py` | `MeetingService.identity`; `create_run` stores `user_id`; public methods take optional `user` |
| Store | `backend/app/meeting/store.py` | `Run.user_id`; `RunStore.get` / `put` / `list` / `append_events` keyword `user_id` |
| Review token | `backend/app/meeting/review_token.py` | `ReviewTokenService.issue` / `validate` keyword `user_id` |
| Feishu | `backend/app/meeting/adapters/feishu.py` | `FeishuWriteRequest.user_id` |
| Factory | `backend/app/meeting/service.py` | `build_meeting_service` calls `build_identity()` |

Code comments marked `# Extension: multi-user` point back here.

`Run.user_id` is inside the existing `snapshot_json` blob. A snapshot
written before this field existed loads as `local`. v1 does not add a
SQL column and does not add Postgres.

The review-token string stays
`v1.{run_id}.{content_hash}.{exp}.{jti}.{sig}`. `user_id` is not in
that HMAC body, so tokens issued today still verify after a restart
when `MEETING_REVIEW_TOKEN_SECRET` is set.

Feishu adapters do not read `FeishuWriteRequest.user_id`. Assignee
strings that are not already `ou_…` open_ids stay unassigned. Display
names are not resolved.

This seam is the meeting package only. It is separate from the Eigent
desktop account (`account_owner_id`) and from Space, Session, and Task.

## Suggested next steps / 建议的后续步骤

Do these in order, on the machine that already runs the API. Keep
`SingleUserIdentity` as the fallback so the mock scripts stay
single-user when the new source is absent.

1. **Identity.** Add a second `IdentityProvider` and return it from
   `build_identity` only when you have a real opaque id. Read
   `presented` (the `X-Meeting-User` value) or `MEETING_USER_ID` in
   that provider, in `current()`. Leave `SingleUserIdentity.current`
   returning `local`. An opaque string is enough for this step. Login
   UI, a user table, and a session cookie can wait.
2. **Scope the store by that id.** `ignore_user_scope` is the hook
   `RunStore` calls today. When you are ready to hide other people's
   runs, stop ignoring the id in `RunStore.get` and `RunStore.list`
   and keep rows whose `Run.user_id` matches. `put` already writes
   `user_id` into `snapshot_json`. Update
   `tests/app/meeting/test_identity.py`, which currently asserts that
   a different id still returns the run.
3. **Feishu per user.** `MeetingService.write_feishu` already sets
   `FeishuWriteRequest.user_id` from the run. Teach the lark-cli
   adapter, or a new adapter, to pick the Feishu identity or an
   `ou_…` assignee from that id. Keep the human review token on the
   write. Keep refusing display-name lookup.
4. **Review tokens, after the three steps above.** Binding a token to
   the user means a new prefix. Leave the `v1` HMAC body as it is so
   tokens already in the SQLite file keep verifying.

`scripts/meeting-dev.sh --check` should keep treating a missing
`MEETING_USER_ID` as the local user for as long as the default path
is single-user.

## Out of scope / 先不做

Auth, tenants, user tables, login UI, multi-tenant Feishu app
mapping, DingTalk, WeCom, contact-name lookup, and a hosted service.
Issue #4 tracks the full product. This page does not implement it.

## Checks / 检查

```bash
cd backend && uv run pytest tests/app/meeting -q
```

`tests/app/meeting/test_identity.py` covers the default provider, the
ignored header and env var, the store hook, and the token body.
