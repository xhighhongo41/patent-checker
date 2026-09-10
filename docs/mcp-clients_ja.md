# 各エージェントへの手動登録

English: [mcp-clients.md](mcp-clients.md)

`patent-checker install` はこれらの設定を代わりに書きます(README を参照)。このページは、自分で書きたいとき、登録を直したいとき、インストーラがスニペットの表示だけを行うエージェントを使うときのためのものです。

すべての例は既定の URL `http://127.0.0.1:8642/mcp` を使います。`${PATENT_CHECKER_SERVER_TOKEN}` は `secrets/server_token.txt`(または `.env`)のトークンに置き換えるか、エージェントが参照を展開する場合はエージェント起動前にその環境変数を export します。トークンは共有の秘密です。実値を持つファイルは他人が読めないようにし(`chmod 600`)、決してコミットしないでください。

| エージェント | インストーラの登録方式 | 書き込み先 |
|---|---|---|
| Claude Code | `claude mcp add` | ユーザー設定、`--scope project` なら `.mcp.json` |
| Gemini CLI | `gemini mcp add` | `~/.gemini/settings.json` または `.gemini/settings.json` |
| GitHub Copilot CLI | `copilot mcp add` | `~/.copilot/mcp-config.json` または `.mcp.json` |
| Cursor | JSON マージ | `~/.cursor/mcp.json` または `.cursor/mcp.json` |
| OpenCode | JSON マージ | `~/.config/opencode/opencode.json` または `opencode.json` |
| Codex CLI | `--token-env` のとき `codex mcp add`、それ以外は TOML 追記 | `~/.codex/config.toml` |
| OpenHands | 貼り付け用スニペット | 作業ディレクトリの `config.toml` |
| Hermes Agent | 貼り付け用スニペット | `~/.hermes/config.yaml` |
| Copilot coding agent | 貼り付け用スニペット(GitHub.com のリポジトリ設定) | リポジトリ設定 |

## Claude Code

セッションの外で実行し、新しいセッションを開始します。

```sh
TOKEN="$(cat /path/to/secrets/server_token.txt)"
claude mcp add --transport http --scope user patent-checker http://127.0.0.1:8642/mcp \
  --header "Authorization: Bearer $TOKEN"
```

`claude mcp get patent-checker` で登録内容を確認でき、`claude mcp remove patent-checker` で削除できます。プロジェクトスコープ(`--scope project`)はカレントディレクトリの `.mcp.json` に書きます。

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## Codex CLI

トークンを環境変数に置く場合(`bearer_token_env_var` は変数名です。Codex は起動時に読みます)。

```sh
codex mcp add patent-checker --url http://127.0.0.1:8642/mcp --bearer-token-env-var PATENT_CHECKER_SERVER_TOKEN
```

または `~/.codex/config.toml` に書きます(プロジェクト単位のファイルは信頼済みプロジェクトでのみ読まれます)。

```toml
[mcp_servers.patent-checker]
url = "http://127.0.0.1:8642/mcp"
bearer_token_env_var = "PATENT_CHECKER_SERVER_TOKEN"
```

後でトークンを変えるときはこのテーブルを編集する(または `codex mcp add` をやり直す)。インストーラは既存の Codex の項目を書き換えません。

## Cursor

`~/.cursor/mcp.json`(ユーザー)または `.cursor/mcp.json`(プロジェクト)。

```json
{"mcpServers": {"patent-checker": {"url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## Gemini CLI

```sh
gemini mcp add --transport http -s user patent-checker http://127.0.0.1:8642/mcp \
  -H "Authorization: Bearer $TOKEN"
```

または `~/.gemini/settings.json`(プロジェクトは `.gemini/settings.json`)。

```json
{"mcpServers": {"patent-checker": {"httpUrl": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## GitHub Copilot CLI

```sh
copilot mcp add --transport http patent-checker http://127.0.0.1:8642/mcp
```

その後 `~/.copilot/mcp-config.json` にヘッダを足します。Copilot CLI はこのファイルでの環境変数参照を文書化していないため、値そのものを書きます。

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer <paste your token>"}, "tools": ["*"]}}}
```

## OpenCode

`~/.config/opencode/opencode.json`(ユーザー)または `opencode.json`(プロジェクト)。

```json
{"mcp": {"patent-checker": {"type": "remote", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer {env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

## OpenHands

作業ディレクトリの `config.toml`。OpenHands の文書は `api_key` がどのヘッダで送られるかを述べていないため、ベアラートークンとしてサーバーに届く保証はありません。最初の呼び出しが 401 で拒否されたら、OpenHands CLI の `--header "Authorization: Bearer ..."` オプションを代わりに使ってください。

```toml
[mcp]
shttp_servers = [{ url = "http://127.0.0.1:8642/mcp", api_key = "<paste your token>" }]
```

## Hermes Agent

`~/.hermes/config.yaml`。Hermes はヘッダ値の `${VAR}` を展開するので、ファイルにトークンを置かず変数を参照できます。

```yaml
mcp_servers:
  patent-checker:
    url: http://127.0.0.1:8642/mcp
    headers:
      Authorization: "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"
```

## GitHub Copilot coding agent

coding agent は GitHub.com 上で動くため、TLS 付きで公開したサーバーにしか到達できません([deploy-lan.md](deploy-lan.md) を参照)。リポジトリの Copilot 設定で MCP 構成を追加し、トークンは `COPILOT_MCP_` 接頭辞の Copilot 環境シークレットとして保存します。

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "https://patents.example.com/mcp",
  "headers": {"Authorization": "Bearer $COPILOT_MCP_PATENT_CHECKER_TOKEN"}, "tools": ["*"]}}}
```

## HTTP ではなく stdio を使う

`patent-checker serve --transport stdio` は標準入出力で MCP を話し、ベアラートークンはありません。エージェントがサーバーを子プロセスとして起動し、あなた自身の権限で動くためで、HTTP が既定なのはこのためです。登録には各エージェントの stdio 形式を使います。例:

```sh
claude mcp add --transport stdio patent-checker -- patent-checker serve --transport stdio
```

```json
{"mcpServers": {"patent-checker": {"command": "patent-checker",
  "args": ["serve", "--transport", "stdio"]}}}
```

運用者告知は stdio でも適用されます。`PATENT_CHECKER_OPERATOR_CONSENT` を環境変数かプロジェクトの `.env` に設定しないとサーバーは起動を拒否します。

## Skill を手で置く

Skill は `SKILL.md` を含むディレクトリです。Python パッケージに同梱されている `patent-checker` ディレクトリ(`patent-checker install --list-agents` が場所を表示します)か、このリポジトリの `skills/patent-checker/` を、エージェントの skills ディレクトリにコピーするだけで手動インストールは完了です。`~/.agents/skills/patent-checker/` は Codex CLI・OpenCode・Cursor・Gemini CLI・Copilot CLI が読みます。Claude Code は `~/.claude/skills/`、OpenHands は `~/.openhands/skills/`、Hermes は `~/.hermes/skills/` を読みます。プロジェクト単位では `.agents/skills/patent-checker/`(Claude Code は `.claude/skills/`)を使います。
