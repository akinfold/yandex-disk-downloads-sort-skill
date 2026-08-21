# Getting a Yandex Disk OAuth token

The scripts authenticate with a personal OAuth token sent as `Authorization: OAuth <token>`.
There are two ways to get one. Option A takes about two minutes and needs no registration.

## Option A: the Poligon page (quickest)

Poligon (https://yandex.ru/dev/disk/poligon/, English mirror https://yandex.com/dev/disk/poligon/)
is Yandex's interactive console for the Disk API. Its "get token" button runs a normal OAuth
authorization for Yandex's own registered application; the token it issues is an ordinary
OAuth token and works from any script.

1. Sign in to the Yandex account whose Disk you want to sort (https://passport.yandex.ru).
   On a shared computer use a private window.
2. Open https://yandex.ru/dev/disk/poligon/ and dismiss the cookie banner if it appears.
3. At the top of the console there is a field **"Ваш OAuth-токен"** ("Your OAuth token") and a
   yellow button **"Получить OAuth-токен"** ("Get OAuth token") next to it. Click the button.
   It opens `https://oauth.yandex.ru/authorize?response_type=token&client_id=…` for the Poligon
   application.
4. Log in if asked. On the consent screen (application name plus the list of Disk permissions)
   click **"Разрешить"** ("Allow"). If you authorized the Poligon earlier and that token has not
   expired yet, the consent screen is skipped.
5. You are redirected back to the Poligon. The page reads the token from its own URL
   (`#access_token=…&expires_in=…`) and puts it into the "Ваш OAuth-токен" field. Copy it from
   the field. A token looks like `y0_AgAAAAA…`: it starts with `y`, a digit and `_`, is about
   58 characters long and contains only letters, digits, `_` and `-`.
6. Store it where the scripts can find it (see "Where to put the token").
7. Verify it:

   ```bash
   curl -sS -H "Authorization: OAuth $YANDEX_DISK_TOKEN" https://cloud-api.yandex.net/v1/disk
   ```

   `200` with JSON (`total_space`, `system_folders`, …) means it works. `401 UnauthorizedError`
   means the token is wrong or expired.

Good to know:

- The Poligon keeps the token in your browser's local storage for the `dev.yandex.net` site.
  On a shared machine clear that site's data afterwards.
- The token carries whatever permissions Yandex granted the Poligon application (the set is
  not published, but it evidently covers reading and writing files and folders, the trash and
  Disk info, since the console does all of that). That is what this skill uses; a `403` on a
  move would mean the write permission is missing, in which case use Option B.
- Lifetime: Yandex tokens are typically valid for up to a year; the exact number of seconds is
  the `expires_in` value in the redirect URL. When it expires, repeat steps 2–6. This flow has
  no refresh token.
- Revoke it at any time at https://id.yandex.ru/personal/data-access ("Доступы к данным") by
  removing the Poligon application. Changing your password, turning two-factor authentication
  on or off, or "log out everywhere" also revokes every token.

## Option B: your own OAuth application

Choose this when you want a token bound to an application you control: to revoke it
independently of the Poligon, to pick the exact scopes, or to obtain refresh tokens.

1. Open https://oauth.yandex.ru/ and click **"Создать приложение"** (Create application). Pick
   **"Для доступа к API или отладки"** (for API access or debugging); the type cannot be
   changed later. The direct link is https://oauth.yandex.ru/client/new/.
2. Fill in **"Название вашего сервиса"** (service name) and **"Контактная почта"** (contact email).
3. Permissions: start typing "Диск" and add
   - `cloud_api:disk.read` — Чтение всего Диска (read the whole Disk),
   - `cloud_api:disk.write` — Запись в любом месте на Диске (write anywhere),
   - `cloud_api:disk.info` — Доступ к информации о Диске (quota and system folders).

   `cloud_api:disk.app_folder` alone confines the app to its own folder and is not enough to
   sort Downloads.
4. The Redirect URI for API-access applications is fixed to
   `https://oauth.yandex.ru/verification_code`. Click **"Создать приложение"** and copy the
   **ClientID** from the application page.
5. Open `https://oauth.yandex.ru/authorize?response_type=token&client_id=<ClientID>` and click
   **"Разрешить"**. The browser lands on
   `https://oauth.yandex.ru/verification_code#access_token=<token>&expires_in=<seconds>`.
   Copy the `access_token` value from the address bar (the page shows it as well).
6. Store and verify it as in Option A.

For tokens that can be refreshed without a browser, use the authorization-code flow:
`response_type=code` in step 5, then `POST https://oauth.yandex.ru/token` with
`grant_type=authorization_code`, `code`, `client_id`, `client_secret`; later refresh with
`grant_type=refresh_token`. Yandex ID documentation: https://yandex.ru/dev/id/doc/ru/
(manual token: `tokens/debug-token`; code flow: `codes/code-url`; device flow:
`codes/screen-code-oauth`).

## Where to put the token

The scripts look, in this order, for `--token-file <path>`, the `YANDEX_DISK_TOKEN` variable,
the `YANDEX_DISK_OAUTH_TOKEN` variable, and a file named by `YANDEX_DISK_TOKEN_FILE`.

- Current shell session: `export YANDEX_DISK_TOKEN='y0_…'`.
- A file: `printf '%s' 'y0_…' > ~/.yandex-disk-token && chmod 600 ~/.yandex-disk-token`, then
  `--token-file ~/.yandex-disk-token` or `export YANDEX_DISK_TOKEN_FILE=~/.yandex-disk-token`.
- Claude Code, Codex and similar: put the `export` in your shell profile or in the `.env` your
  launcher loads. Never in a file inside a repository.
- ChatGPT Custom GPT: paste `OAuth y0_…` as the action's API key (see `chatgpt/README.md` in
  the repository).

Never paste the token into a chat, an issue, a commit or a log. The scripts never print it.

## Common errors

| Error | Cause | Fix |
|---|---|---|
| `401 UnauthorizedError` | Token missing, mistyped, expired, revoked, or sent with the `Bearer` scheme | Get a new token; always use `Authorization: OAuth …` |
| `403` with "forbidden" / "недостаточно прав" | Token lacks the write scope (for example `disk.app_folder` only) | Re-issue with `disk.read` + `disk.write` + `disk.info` |
| `404` for `disk:/Загрузки` on a fresh account | The Disk creates system folders lazily | Save any file to Disk once, or pass `--path` |
| `CERTIFICATE_VERIFY_FAILED` on macOS | python.org Python without root certificates | Run `Install Certificates.command` from the Python folder, or `pip install certifi`, or `export SSL_CERT_FILE=/etc/ssl/cert.pem` |
