# Using the skill from ChatGPT

ChatGPT's code interpreter has no network access, so it cannot run the bundled scripts
against Yandex. The ChatGPT version is therefore a **Custom GPT with an Action**: ChatGPT calls
the Yandex Disk REST API itself, following the same workflow and sorting rules as the skill.

Files in this directory:

- `openapi.yaml`: the Action schema (five operations: `getDiskInfo`, `listResource`,
  `createFolder`, `moveResource`, `getOperationStatus`; deliberately no delete, upload or
  download).
- `instructions.md`: the GPT's instructions (about 6,500 characters; the editor allows 8,000). Its category block is generated from `assets/rules.default.json` and a test keeps the two in sync.

## First: can you create a GPT?

Since August 2026 new GPTs can be created only in ChatGPT Business, Enterprise and Edu
workspaces. Existing GPTs on personal plans keep working (editing requires an eligible
subscription). If you cannot create one:

- **Codex** (CLI, IDE extension, ChatGPT desktop app) runs the very same skill folder with
  scripts; see the main README, section "Install the skill".
- **Developer mode + MCP** in ChatGPT lets you connect a remote MCP server that wraps the Yandex
  Disk API. Such a server is not part of this repository, and on personal plans (Plus/Pro) the
  help center currently allows only read/fetch MCP tools; write tools are in beta for
  Business, Enterprise and Edu.

## Build the GPT

1. ChatGPT → GPTs → **Create** (or open your GPT → **Edit**) → **Configure**.
2. Name it, e.g. "Yandex Disk Downloads Organizer". Conversation starters that work well:
   "What's in my Downloads on Yandex Disk?", "Sort my Downloads folder", "Find duplicates in
   Downloads", "Что лежит в Загрузках на Диске?".
3. **Instructions**: paste the contents of `instructions.md`.
4. Capabilities: none are needed (web search, canvas, image generation, code interpreter can all
   stay off).
5. **Actions → Create new action → Schema**: paste `openapi.yaml` (or *Import from URL* with the
   raw GitHub URL of the file). The editor lists the five operations if the schema is valid.
6. **Authentication** (gear icon above the schema):
   - Authentication Type: **API Key**
   - Auth Type: **Custom**
   - Custom Header Name: `Authorization`
   - API Key: `OAuth y0_…` — the word `OAuth`, one space, then your token. Get a token in two
     minutes: [`references/oauth-token.md`](../skills/yandex-disk-downloads-sort/references/oauth-token.md).

   Yandex requires the `OAuth` scheme, so the built-in *Bearer* option does not work, and the
   editor's OAuth option cannot be used either (Yandex's authorization server and API live on
   different domains, which the editor rejects).
7. Privacy policy URL: only required if you publish the GPT. Keep it **Only me**: the token is
   stored inside the GPT, so anyone who can use the GPT can act on your Disk.
8. Test: click **Test** next to `getDiskInfo`; a `200` with your quota confirms the token. Then
   in *Preview* ask "What's in my Downloads?".

## What to expect

- `getDiskInfo`, `listResource` and `getOperationStatus` are read-only and marked
  non-consequential, so ChatGPT can offer "Always allow" for them.
- `createFolder` and `moveResource` are consequential: ChatGPT asks before every call. The
  instructions additionally require that you confirm the plan before the first move.
- A folder with hundreds of files means hundreds of confirmed `moveResource` calls. For big
  cleanups the script version (Claude Code, Codex) is far more comfortable; the GPT is great
  for analysis and small batches.
- Responses are capped at 100,000 characters and 45 seconds per call; the instructions ask
  for only the needed fields and 200 items per page to stay within that.

## Keeping the schema in sync

`openapi.yaml` is validated in CI with `openapi-spec-validator`. Operation summaries and
descriptions are kept under 300 characters and parameter descriptions under 700, the limits
the GPT editor enforces.
