# Services, conversations and company knowledge

Read this reference for model-service setup, per-agent conversations, explicit cross-conversation context, or knowledge-library deletion. Use the installation's saved workspace; keep all persistent data and native CLI state under that root.

## Service and executor choice

“My Model Services” appears below the partner list in both views. Create a named service, explicitly choose `codex` or `claude-code`, then choose `api_key` or `account`. Never infer the executor from a URL/model name. A service can be deleted directly even when partners refer to it. The deletion atomically detaches provider/slot/model/effort from those profiles and returns them to “configure later”; it never deletes their conversations. Already captured invocations continue with their original runtime credentials/driver. Retire those credentials only after all such invocations finish; do not terminate their shared account driver during deletion. Queued work follows the configuration present when it begins. A profile cannot be newly rebound to an archived service. All configure-later controls must submit null provider/model/effort without waiting for catalog or Key availability.

| Mode    | Address and credentials                                                                                        | Restart behavior                     |
| ------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------ |
| API Key | Custom provider/proxy URL plus webpage password field; Codex requires Responses, Claude Code requires Messages | Enter Key again                      |
| Account | Empty stored service URL; official authorization/inference endpoints                                           | Complete account authorization again |

Codex account login uses the native ephemeral app-server device flow: display the actual URL/device code returned for this attempt and poll backend status. Claude Code uses the actual official PKCE authorization URL; the user pastes the returned authorization code into the dedicated webpage form for exchange. Do not ask for this code in agent chat or put it in URLs, notes or files. A visited website is not a completed login; display completion only after backend confirmation. Cancelled, failed and expired flows remain distinct from completed authorization. Do not route arbitrary API proxy URLs through a fake OAuth flow.

Keys, OAuth tokens, PKCE verifier and authorization codes are runtime memory only. Saved service metadata is nonsecret. Browser reload/tunnel reconnect retains the running runtime's credentials; core restart clears them. A valid 30-day browser login survives restart independently. Do not use global CLI login files or another user's launcher/auth/settings. For CLI discovery, use installed Codex and/or Claude as selected by the service. The matching CLI is a machine prerequisite and is not bundled. Claude can be selected through local `CLAUDE_CLI`, PATH, or the workspace's nonsecret `cliPaths["claude-code"]`. Do not package machine-local CLI paths or login material.

The partner editor places name/job first, then a service selector defaulting to “configure later”; model/effort controls appear only after selection. It has no gender label, and retains Claw appearance selection. Partners bind the service's runtime credential slot automatically. Model listing proves visibility, not successful inference. Running calls retain their captured executor/provider/model/effort/credential/conversation configuration.

## Conversation identity and persistence

Each agent has its own conversation list with create, select and copy-ID actions. Rename through the knowledge library. Messages, progress, idempotency and task/stop ownership are scoped by conversation ID. Switching does not stop an active task; another conversation shows that the partner is working elsewhere rather than borrowing its summary/stop button. A single agent queues its work; there is no fixed global task-concurrency cap.

Native thread/session bindings include agent, conversation, provider and executor. New conversations start independently; old conversations resume their corresponding native binding. Data stays in the chosen root and survives restart. Legacy messages retain their original IDs, ordering/timestamps, content and attachments when migrated to a stable default conversation. An old Codex thread is not resumed as Claude. Deleted IDs leave tombstones and are not reused, including a deleted legacy default ID.

## Metadata directory and explicit context

The clickable knowledge room is to the right of the work area; minimal chat also has a library entry. The directory groups by agent and supports search, category/member filters, title edits, existing/custom categories, summary, last-used time and readers. Archived agents' conversations remain discoverable. Initial browsing fetches metadata only; “View details” fetches the body. Browser browsing does not count as an agent read.

Explicit selection imports read-only snapshots into the target conversation, with source ID/name/title/import time/message count/excerpt. At most four source conversations may be referenced; each snapshot contains the latest 12000 characters, with a truncation notice when needed. This bound limits prompt context, not task concurrency. A snapshot does not silently refresh when the source changes, and references can be removed. No whole-company history is injected by default; historical instructions remain source material.

The MCP tools `office_search_conversations` and `office_read_conversation` support metadata discovery and bounded/paginated reads by conversation ID. Snapshot imports and actual agent reads record reader/count/time plus affected targets; they do not fabricate reads from a user preview. Read tracking lets deletion invalidate affected native context caches.

## Permanent deletion and pending cleanup

Require the visible irreversible confirmation. An active task in the affected agent/conversations/account service blocks deletion; stop relevant tasks first. The current chat cannot be deleted from its own context picker until switching away. Deletion is scoped to managed data:

- Remove the source conversation's DB messages/progress/idempotency/thread mapping and metadata; retain the deleted-ID tombstone.
- Purge corresponding native history, thread/session metadata and managed logs/handoff sections, plus attachments that are no longer referenced.
- Purge source/target context snapshots and read associations. Reset affected native caches so removed source text cannot return through a prior native resume.
- Preserve other conversations' already generated messages and user project artifacts. Removing a source cannot undo text already incorporated into those outputs.

Cleanup only handles verified root-local managed formats/paths. The current file-cleanup mechanism requires Linux; unknown native formats, unsafe links or changed files must fail rather than trigger broad directory removal. A durable journal survives partial failure. The journal is written before DB deletion, so the DB record may still exist or may already be gone; reload server metadata rather than optimistically removing a row. A cleanup error must not be described as “nothing was deleted” or “all files were deleted.”

`GET /api/office/conversation-cleanups` lists pending cleanup jobs. The library shows “存储清理未完成” with a message and explicit retry button; that button invokes `POST /api/office/conversation-cleanups/:id/retry`. `conversation_cleanup_pending` is shown safely as “会话删除或存储清理尚未完成，请在知识库重试清理。” After a delete with this result, reload metadata plus pending jobs; remove selection/preview only if refreshed metadata confirms the conversation is absent. Other delete errors retain the record. Successful retry reloads both lists; failed retry retains the pending job and an honest failure message. Retry can still fail on an unsupported native format.

Cleanup temporarily locks new execution for involved agents/account providers; this is a maintenance lock, not a global concurrency cap. Startup attempts durable recovery, preserving pending jobs on cleanup failure so the remaining office can start. Do not run deletion or retries as installation probes, and do not erase a journal to silence a warning.

## Evidence and reuse

Describe current implementation separately from the results actually recorded for the candidate package. Offline structure/hash checks, compile/build, short model connectivity, browser workflows, OAuth, destructive cleanup, full 1M input and clean installation are distinct evidence. Do not reuse an old bundle's results for a rebuilt package. Each user chooses their own root, credentials and execution mode; another machine's account authorization does not transfer. Commands are maintained in [workflows](workflows.md).

## Dismissed employees

The archive area is called “小黑屋” and appears at the lower right of the office; see the [feature overview](../README.md). Active employees offer “删除身份”: stop managed tasks, remove from the active office and meetings, and archive with configuration preserved. Only the blackroom offers “开除员工”. Final dismissal removes the employee from both visible lists and clears identity configuration, retaining only ID/name/prior role needed for historical attribution. Preserve conversations, native histories and company project outputs. Historical-owner metadata has a separate minimal API and must not repopulate the room. The Boss cannot be archived or dismissed. Shared provider credentials remain company services; revoke a former personal slot only when neither an employee nor an active service references it. Older archives already cleared by a previous release cannot reconstruct their lost configuration.

### Claude-compatible relays

Treat Claude CLI context suffixes `[1m]`/`[2m]` separately from the wire model ID; authorize only the configured base model or documented official alias. For a custom API model, bind main and auxiliary model defaults to that model. Do not read an existing server credential file into this installation; collect the Key in the webpage. An `ANTHROPIC_AUTH_TOKEN` relay uses Bearer authentication, unlike the official `x-api-key` API. Enumeration tries bounded same-origin catalog paths, including sibling catalogs when Messages uses `/anthropic`; a missing catalog is not evidence that inference is unavailable. Keep manual model entry in identity configuration and mark unknown reasoning/context capability unverified. Never substitute a hardcoded model list for the user's remote service.

All office rooms have real spacing and visible connecting corridors, including the dismissed-employee room; movement follows doors/corridors. The service sidebar is the sole service-management entry; remove the old runtime-credential tip link.

Claude SDK 2.1.311 sends Messages and count_tokens with `?beta=true`. Authorize pathname separately from query: preserve only the supported single `beta=true` for Messages routes; keep method/run/model/route restrictions. Comparing the complete URL to `/messages` rejects locally with 404, which the CLI misleadingly reports as model_not_found before any upstream call. For 1M selection, identity and chat controls expose a manual checkbox while catalog IDs stay undecorated; persist the CLI suffix, restore it on reopen, clear on provider change, and apply only to future tasks. A short successful annotated request does not establish actual million-token capacity.
