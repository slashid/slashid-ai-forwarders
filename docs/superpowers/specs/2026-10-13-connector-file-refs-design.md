# Connector file references on `AIAccessedFile`

**Date:** 2026-10-13
**Status:** Design. Every shape below was captured on 2026-10-13 from live Google Drive and Microsoft 365 connectors, through claude.ai (`claude-work`) and Codex.
**Companion changes:** the Codex tool-call extraction in `2026-10-13-codex-tool-calls-design.md`; in `ng-evangelion`, `file_ref` in the batched spec sync, and an index on the GUID and path of `MicrosoftM365OnedriveFileMetadata` (see Server).

## Problem

"Sensitive file shared with AI" matches `accessed_files` by content hash. Files read through a cloud connector cannot be matched that way:

- Drive `read_file_content` returns a re-rendering, never the stored bytes. Markdown and plain text come back markdown-escaped (`\#`, `\*\*`, a trailing double space per line, `.txt` included), `.pdf` and `.docx` as extracted text, native Docs as markdown. Un-escaping was rejected: the content differs by preprocessing, not by encoding.
- SharePoint `read_resource` returns the raw text for `.md` and extracted text for `.pdf`/`.docx`.
- Claude Code replaces a result over its token cap with an `Error: result (N characters) exceeds maximum allowed tokens…` string, and Codex truncates long `exec` output; neither carries the content.

The provider's file ID is in the call and is what an admin copies from a share link. Each format of a document is its own file with its own ID; grouping sibling formats is out of scope.

## Wire

`AIAccessedFile.file_ref: str | None`, a typed string: the identifier the call exposed, the first of `item`, `guid`, `path` when it exposed several.

| Ref | Meaning |
| --- | --- |
| `google/<fileId>` | Drive file ID |
| `m365/item/<driveId>/<itemId>` | Graph drive item |
| `m365/guid/<GUID>` | SharePoint file GUID (uppercase, no braces), from a `sourcedoc` link |
| `m365/path/<driveId>/<path>` | Path relative to the drive root, library name omitted, percent-decoded once (`Recipes/Pipoca %#.txt`) |

A Codex SharePoint fetch can expose an `itemId` (`document_id`) and a GUID (`web_link`); the `item` is kept. The forwarder derives nothing across kinds; the one transformation is decoding a path. `name`, `media_type` and `provenance: tool_result` are set as for local files. The server ignores unknown fields until the spec sync lands.

## Recognition

One recognizer takes `(connector action, arguments, structured result | None)` and returns files; adapters feed it. It runs where tool results become `accessed_files` (`extract_tool_result_files` today matches only the exact `_READ_TOOLS` names, so it gains a connector branch). A `tool_result` with `tool_is_error` is skipped, as for local reads. An unparseable or oversize result still yields the ref found in the arguments. `name` is the result's (`title` for Drive, `file.name` for M365) or, without a result, the last path segment; else `None`.

**Adapters.**
- claude.ai and the API: the `tool_use` and `tool_result` of the normalized messages. The server name varies (`claude_ai_Google_Drive`, a custom connector), so the name is matched as `mcp__<server>__<tool>` with a server whose name identifies the connector (`Google_Drive`, `Microsoft_365`) and the exact tool.
- Codex: the synthesized pairs of the Codex design, `mcp__codex_apps__<connector>__<tool>` with `google_drive` or `microsoft_sharepoint`. The result is `{content, structuredContent, isError}`; use `structuredContent`.
- Codex preflight: the `PreToolUse` hook, which has the arguments but no result (see Preflight).

| Connector | claude.ai tool | Codex tool (after `mcp__codex_apps__`) | Refs from |
| --- | --- | --- | --- |
| Drive | `read_file_content`, `get_file_metadata`, `download_file_content` | `google_drive__fetch`, `google_drive__get_file_metadata` | `fileId` argument. Codex `fetch` takes `url` (`/d/<id>/` of a `drive.google.com` or `docs.google.com` URL) and returns `id` |
| Drive | `search_files` | `google_drive__search` | `files[].id` / `results[].id`: the snippets reach the model |
| M365 | `read_resource` | n/a | `uri` `file:///<driveId>/<rest>`: if `<rest>` is one segment matching `^01[A-Z2-7]{30,}$` → `item`, otherwise → `path` (percent-decoded; `<driveId>` is used as sent, `b!…` has nothing to decode); anything else (`page:`, `mail:`, …) is not recorded |
| M365 | `sharepoint_folder_search` | `microsoft_sharepoint__search` | result `driveId`+`id` / `drive_id`+`item_id`, as for Drive search |
| M365 | n/a | `microsoft_sharepoint__fetch` | `url`: Graph `…/drives/<d>/items/<i>` → `item`; `Doc.aspx?sourcedoc=%7B<GUID>%7D` → `guid`; a path URL gives no ref. Result `document_id` / `web_link` likewise |

M365 folder listings (`<name> (file|folder, N bytes) file:///…`) are not recorded. `get_file_permissions`, `share_file` and `trash_file` expose no content and are not recorded.

A `read_resource` path that includes the library segment (`Shared Documents/…`) fails with `NOT_FOUND` and is dropped as an error, so every recorded path is already relative to the drive root. A malformed percent-encoding fails with `VALIDATION_ERROR` and is dropped likewise.

## Hashing

- Every Drive `download_file_content` payload (claude.ai: `content`, base64, beside `id`, `mimeType`, `title`) and Codex `fetch` `b64_string` (only with `download_raw_file` and `include_base64`) is hashed, whatever its type and whatever `exportMimeType` was sent. A stored file's download is its bytes: the stored `sha256` of `banana-bread.md` equals the hash of its download, and the same PDF hashes identically through claude.ai and Codex (`9591f9d11032…`). A native file's export (`application/vnd.google-apps.*`) has no stored counterpart, so its hash matches nothing, or matches identical content; it still matches on its ref.
- `read_file_content` and a default `fetch` are re-renderings and are not hashed.
- M365 content is not hashed in the first version: the result carries no type on claude.ai, and extracted text never matches.
- An oversize result is not hashed: Claude Code replaces it with the `Error: result (N characters) exceeds…` string, which is not base64. The `max_file_bytes` limit and the per-request file and byte limits of local hashing apply. Codex's `structuredContent` is not truncated.

## Preflight

Codex's `PreToolUse` fires for every tool called inside an `exec` script, with the call's arguments and the `exec-<uuid>` id of the later `McpToolCall`. The preflight runs the recognizer on the arguments alone: a Drive `fetch` URL, a SharePoint `fetch` Graph URL or `sourcedoc` link yields its ref before the call runs, with no hash and no result-only ref (`document_id`, search results). The same file's event entry, built after the call, carries the full ref.

## Dedup

Connector entries sharing a `file_ref` merge, never on name. The merged entry keeps the hashed entry's hashes and takes `name` and `media_type` from whichever has them; with equal or no hashes the earliest wins. Today `_dedupe_by_name_hash` is first-seen-wins on `(name, alg, value)`, and a preflight record (ref-only, first) must not beat the later hashed entry, so the merge is a new step beside it. Entries for one file with different refs (a path read and an `itemId` read) stay separate; the server matches each on its own ref.

## Identity facts (M365)

- A SharePoint/OneDrive-for-business `itemId` contains the file GUID (base32-decode the part after `01`, skip 4 bytes, read 16 as a little-endian GUID); it equalled the `sourcedoc` GUID on 13 of 13 files from the API (12 SharePoint `.docx`, one OneDrive `.xlsx`). The 4 skipped bytes differ per drive, so a GUID cannot be turned back into an `itemId`.
- Only Office files get a `Doc.aspx?sourcedoc=` `web_link`. `.md`, `.txt` and `.pdf` get a path link with the library name, URL-encoded (`…/sites/SlashID/Shared%20Documents/Recipes/banana-bread.md`). So a `guid` ref only occurs for Office files, and the derivation is unchecked for the rest.
- A `sourcedoc` link carries no `driveId`, so a GUID ref is drive-less.
- Not every call has an `itemId`: `read_resource` by path returns the text and no ID (a whitespace-only file's result also carried `file.name` and a path-based `webUrl`; unchecked on other files); a Codex fetch of a `Doc.aspx?sourcedoc=` link returns only that URL. A Codex fetch of a path URL with spaces is rejected by the tool.
- Paths: the API returns them raw, with no escaping, and OneDrive forbids `\/<>:*"?` in names, so `/` is always a separator. `read_resource` requires the path percent-encoded (a literal `%` as `%25`, `#` as `%23`, else a fragment) and decodes it; `Pipoca %#.txt` reads through `Pipoca%20%25%23.txt` only. Matching is case-insensitive. The library segment is not stripped by the connector.
- Drive: `fileId` is on every call seen, as argument or result.
- Not covered: share tokens (`:t:/s/…/IQ…`; resolving them needs Graph), moved or renamed files for path refs, libraries other than the default one. OneDrive Personal cannot occur: the connector's app registration is not enabled for personal accounts.

## Server

Stored M365 file metadata has `drive_id`, `identifier_from_source` (the `itemId`), `web_url` (with the `sourcedoc` GUID for Office files), `parent_path` and `name`. Neither GUID nor path is indexed today. Expand `MicrosoftM365OnedriveFileMetadata` with:

- the file GUID, derived from `identifier_from_source` at ingest (or read from `web_url`);
- a normalized path: lowercased, no leading slash, relative to the drive root (`parent_path` after `root:` + `/` + `name`).

Match each ref kind on its index: `item` on `drive_id` + `identifier_from_source`, `guid` on the GUID, `path` on `drive_id` + lowercased path. The server lowercases the ref and never decodes: the forwarder sends a decoded path. Derivation lives only server-side so a wrong guess is fixed in one place.

## Not covered

M365 content hashing; M365 content that is not a drive file (site pages, Loop, OneNote, Whiteboard, Forms, list items: unverified against a tenant); Teams/Outlook messages and attachments.
