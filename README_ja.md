# Patent Checker

English: [README.md](README.md)

Patent Checker は、あなた自身のソフトウェアプロジェクトに関係しうる公開特許を探索し、見つけたことを日付入りの手順記録形式のレポートとして書き留めるためのツールです。2 つの部分から成ります。

- **MCP サーバ**: 公開特許データ(EPO Open Patent Services と Google Patents)を決定論的に取得します。検索、書誌情報、クレーム、法的状態、パテントファミリーに加えて、オフラインの補助ツールも提供します。
- **Agent Skill**: あなたの AI コーディングエージェント(Claude Code、Codex CLI、Cursor、Gemini CLI、GitHub Copilot CLI、OpenCode、OpenHands、Hermes Agent)を、判断を要する作業へと導きます。コードベースを読み、その機能を特許の語彙に翻訳し、候補をスクリーニングし、レポートを書きます。

Patent Checker は、**何かが特許を侵害しているかどうかを判定しません**。レポートに含まれるのは観察の記録、範囲の記述、未解決の問いであり、判定は決して含まれません。

**状態: プレリリース(v0.5、デプロイ環境整備と初回公開)。**

## 重要なお知らせ

何かをインストールする前に、以下をお読みください。ツールは一度だけこれらへの同意を求め、同意したことを記録します。

- **ここで知ったことは、あなたに不利に働くことがあります。** レポートは、あなたが特定の日に特定の特許を知っていたことを記録します。一部の法域、とくに米国では、特許を知っていたことが故意侵害の主張や損害賠償額の増額の根拠となりえます。一方で、どのように対応したかの日付付きの記録は、あなたに有利に働くこともあります。Patent Checker では、記録の保存場所と、それをバージョン管理で追跡するかどうかを選べます。その選択は意識的に行ってください。
- **検索結果は不完全です。** 網羅的な特許検索というものは存在しません。カバー範囲は国、言語、公開段階、データソースによって異なり、最近の出願はまったく含まれないこともあります。ある文書がレポートに載っていないことは、関連する特許が存在しないことの証拠には決してなりません。
- **これは検索の補助であり、助言ではありません。** Patent Checker は資格を持つ弁理士や特許代理人の代替にはならず、作者はその出力に基づいて行われた、あるいは行われなかった判断について一切の責任を負いません。あなたのプロジェクトについての最終判断と、それへの対応は、あなた自身のものです。

3 つの通知は、それぞれが意味を持つ時点で表示されます。**Skill** は探索を実行する人に上記の通知を表示し、その同意をローカルに記録します(この記録はワークフローが最初に確認するものです)。**サーバ**は、データソースの利用条件、資格情報、サーバが保存する内容についての別個の運用者向け告知を運用者が読むまで、起動を拒否します。**インストーラ**は、Skill が同意の記録を既に見つけられるように、ユーザー通知を最初に表示します。

## 仕組み

```
your agent ──(Skill: judgment)──► patent-checker MCP server ──► EPO OPS
   │                                   │  deterministic fetch,      Google Patents
   │  reads your code, writes           │  normalisation, cache
   ▼  the report                        ▼
.patent-checker/reports/…        per-user document cache
```

1. Skill がコードベースを読み、クレームされうる技術的特徴を列挙し、それらを特許の語彙に翻訳して検索クエリを組み立てます。
2. MCP サーバがそのクエリを EPO OPS に対して実行し、候補文書(クレームは Google Patents から、書誌情報と法的状態は OPS から)を取得して、すべての文書をキャッシュします。同じものを二度取得することはありません。
3. Skill が候補を段階的にスクリーニングし、クレームの構成要素をあなたの機能に対応付けて、レポートを書きます。何を検索したか、何が見つかったか、各候補があなたのコードとどう関係するか、そして何が*カバーされなかった*かを記します。

サーバが受け取るのは検索式と公報番号だけです。あなたのソースコードとプロジェクトの説明があなたのマシンの外に出ることはありません。分析はあなたのエージェントの内部で行われます。

## 前提条件

- **Agent Skills と MCP に対応した AI コーディングエージェント**: Claude Code、OpenAI Codex CLI、Cursor、Gemini CLI、GitHub Copilot CLI、OpenCode、OpenHands、Hermes Agent のいずれか。
- **Python 3.12 以降と [uv](https://docs.astral.sh/uv/)**: コマンドラインツール用です(`pipx` や、仮想環境内での通常の `pip install` でも動作します)。
- **Docker と Compose v2**: サーバをコンテナで動かす場合(推奨)。Docker がなくても、サーバは通常のプロセスとして動作します。
- **EPO Open Patent Services のアカウント**(推奨): <https://developers.epo.org/> で登録し、アプリを作成してコンシューマキーとシークレットを取得します。**早めに申請してください。承認は手動で行われ、通常 1 営業日ほどかかります。** OPS の資格情報がない場合、サーバは*縮退モード*で起動します。エージェントが別の手段で見つけた公報番号についてのクレームは Google Patents から取得できますが、検索、書誌情報、ファミリー、法的状態は利用できず、探索のカバー範囲はそのぶん狭まります。

## サーバのインストール

どちらの方法でも、運用者向け告知への同意とベアラートークンが必要です。トークンは控えておいてください。MCP クライアントにも同じ値が必要です。

### 方法 A: Docker Compose(推奨)

```sh
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/compose.yaml
mkdir secrets

# EPO OPS の資格情報(縮退モードにするなら両方のファイルを空のままにする)
printf '%s' 'YOUR_OPS_CONSUMER_KEY'    > secrets/ops_key.txt
printf '%s' 'YOUR_OPS_CONSUMER_SECRET' > secrets/ops_secret.txt

# すべての MCP クライアントが提示しなければならないベアラートークン
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/server_token.txt

# 運用者向け告知を読み(日本語で表示するには --lang ja を付ける)、承認する
docker compose run --rm patent-checker serve --show-operator-notice
echo 'PATENT_CHECKER_OPERATOR_CONSENT=1.0' >> .env

docker compose up -d
curl -fsS http://127.0.0.1:8642/health
```

イメージは `ghcr.io/xhighhongo41/patent-checker` と `docker.io/xhighhongo41/patent-checker` で公開されており、タグは `X.Y.Z`、`X.Y`、`latest` です。`compose.yaml` は、それが同梱されたリリースの `X.Y` タグに追従します。コンテナは内部で `0.0.0.0` にバインドしますが、Compose はポートを**あなたのマシンのループバックにのみ**公開します(`127.0.0.1:8642`)。サーバは `localhost` と `127.0.0.1` の Host ヘッダだけを受け付け、それ以外は受け付けません。取得した文書、検索結果、リクエストログは名前付きボリューム `patent-checker-data` に保存されます。

シークレットファイルは起動時に一度だけ読み込まれます。OPS のファイルが空なら「未設定」とみなされ、サーバは縮退モードになります。`secrets/README.md` に、ファイルのパーミッションと Windows(PowerShell)での同等の手順を記載しています。

### 方法 B: Docker を使わない

```sh
uv tool install patent-checker          # または: pipx install patent-checker
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/.env.example
cp .env.example .env                    # OPS のキー/シークレットとトークンを記入する
patent-checker serve --show-operator-notice   # 日本語で表示するには --lang ja を付ける
# その後、.env に PATENT_CHECKER_OPERATOR_CONSENT=1.0 を設定する
patent-checker serve
```

`patent-checker serve` は `http://127.0.0.1:8642/mcp` で Streamable HTTP を話し、`PATENT_CHECKER_SERVER_TOKEN`(または `PATENT_CHECKER_SERVER_TOKEN_FILE`)で与えたベアラートークンを要求します。`.env` ファイルは、サーバを起動したディレクトリ(またはその親ディレクトリ)から読み込まれます。環境に既にある変数が優先されます。データはユーザーごとのデータディレクトリ(Linux と macOS では `~/.local/share/patent-checker`、Windows では `%LOCALAPPDATA%\patent-checker`)に保存されます。

`--transport stdio` は、ネットワークポートも認証もなしでサーバを動かします。ローカルの単一クライアント専用です。この場合、サーバはあなたのユーザー権限で動作します。HTTP が既定になっているのはそのためです。

## Skill のインストールとエージェントの接続

1 つのコマンドで、コマンドラインツールのインストール、ユーザー通知の表示、エージェントが読む場所への Skill のコピー、各エージェントへの MCP サーバの登録を行います。

```sh
curl -LsSf https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.ps1 | iex"
```

先にスクリプトを読みたい場合は、`| sh` の代わりに `| more` で取得してください。スクリプトを使わなくても構いません。スクリプトが行うのは uv とツールのインストールだけで、その後はインストーラを自分で実行するよう案内します。

```sh
uv tool install patent-checker
patent-checker install
```

`patent-checker install` は次のことを行います。

1. ユーザー通知を表示して同意を求めます(日付と通知のバージョンとともに `~/.config/patent-checker/consent.json` に記録されます。再度求められるのは通知が変わったときだけです)。
2. Skill を `~/.agents/skills/patent-checker/`(Codex CLI、OpenCode、Cursor、Gemini CLI、Copilot CLI が読む場所)と、専用ディレクトリが必要なエージェントの専用ディレクトリ(`~/.claude/skills/`、`~/.openhands/skills/`、`~/.hermes/skills/`)にコピーします。
3. 検出した各エージェントにサーバ(`--url` を指定しない限り `http://127.0.0.1:8642/mcp`)を登録します。エージェント自身の `mcp add` コマンドがあればそれを使い(Claude Code、Gemini CLI、Copilot CLI)、なければエージェントの JSON または TOML 設定を編集し(Cursor、OpenCode、Codex CLI)、設定に手を触れないエージェント(OpenHands、Hermes Agent)については貼り付け用のスニペットを表示します。

便利なオプション: `--list-agents`(何がどこにインストールされるかを表示)、`--dry-run`(書き込み以外のすべてを実行)、`--agent claude-code --agent cursor`(自動検出の代わりに指定。`--agent all` で対応するすべてのエージェント)、`--scope project`(ホームディレクトリではなく現在のプロジェクトにインストール)、`--lang ja`(日本語の通知)、`--no-skill` / `--no-mcp`。

### ベアラートークンを露出させずに渡す

インストーラはトークンをコマンドライン引数としては受け取らないため、トークンがシェルの履歴に残ることはありません。次のいずれかの方法で渡してください。

```sh
patent-checker install --token-file /path/to/secrets/server_token.txt
# または
export PATENT_CHECKER_SERVER_TOKEN="$(cat /path/to/secrets/server_token.txt)"
patent-checker install
# または、そのまま実行する: インストーラがトークンの入力を(エコーせずに)求める
```

既定では、トークンの値は各エージェント自身の設定ファイルに書き込まれます(プラットフォームが対応していれば、所有者のみがアクセスできるパーミッションで作成されます)。`--token-env` を付けると、インストーラは値の代わりに環境変数 `PATENT_CHECKER_SERVER_TOKEN` への*参照*を、各エージェントが展開する記法で書き込みます。これによりファイルが値を保持することはなくなりますが、エージェントを起動する前にその変数をエクスポートする必要があります。Copilot CLI と OpenHands はこのような参照をドキュメント化していないため、`--token-env` はこれらについては代わりにスニペットを表示します。エージェント自身の CLI で登録する場合、呼び出しの間はそのプロセスの引数にトークンが現れます。

インストーラが表示する登録コマンドやスニペットにトークンが含まれることはありません。`${PATENT_CHECKER_SERVER_TOKEN}` またはプレースホルダが使われます。

### サーバが 401 を返す場合

エージェントが送っているトークンが、サーバの起動時に指定したものと完全に一致しているか確認してください(Claude Code では `claude mcp get patent-checker` で登録済みのヘッダが表示されます。`<token>` のようなプレースホルダがそのまま登録されていないか確かめてください)。サーバが起動していない場合は 401 ではなく接続拒否のエラーになるので、両者は簡単に区別できます。

### 手動での登録

<details>
<summary>エージェントごとの設定(インストーラが書き込む内容)</summary>

`${PATENT_CHECKER_SERVER_TOKEN}` をトークンに置き換えるか、エージェントが参照を展開してくれる場合はその変数をエクスポートしてください。

**Claude Code**(セッションの外で実行し、その後新しいセッションを開始します):

```sh
TOKEN="$(cat /path/to/secrets/server_token.txt)"
claude mcp add --transport http --scope user patent-checker http://127.0.0.1:8642/mcp \
  --header "Authorization: Bearer $TOKEN"
```

プロジェクトスコープでは `.mcp.json` に書き込まれます。

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**Codex CLI**(`~/.codex/config.toml`。プロジェクト内のファイルは信頼済みのプロジェクトでのみ読み込まれます):

```toml
[mcp_servers.patent-checker]
url = "http://127.0.0.1:8642/mcp"
bearer_token_env_var = "PATENT_CHECKER_SERVER_TOKEN"
```

**Cursor**(`~/.cursor/mcp.json` または `.cursor/mcp.json`):

```json
{"mcpServers": {"patent-checker": {"url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**Gemini CLI**(`gemini mcp add --transport http patent-checker http://127.0.0.1:8642/mcp --header "Authorization: Bearer $TOKEN"`、または `~/.gemini/settings.json`):

```json
{"mcpServers": {"patent-checker": {"httpUrl": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**GitHub Copilot CLI**(`copilot mcp add --transport http patent-checker http://127.0.0.1:8642/mcp --header "Authorization: Bearer $TOKEN"`、または `~/.copilot/mcp-config.json`):

```json
{"mcpServers": {"patent-checker": {"type": "http", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer <paste your token>"}}}}
```

**OpenCode**(`~/.config/opencode/opencode.json` または `opencode.json`):

```json
{"mcp": {"patent-checker": {"type": "remote", "url": "http://127.0.0.1:8642/mcp",
  "headers": {"Authorization": "Bearer {env:PATENT_CHECKER_SERVER_TOKEN}"}}}}
```

**OpenHands**(作業ディレクトリの `config.toml`。`api_key` がベアラートークンとして送られるかどうかはドキュメント化されていないため、最初の呼び出しで確認してください):

```toml
[mcp]
shttp_servers = [{ url = "http://127.0.0.1:8642/mcp", api_key = "<paste your token>" }]
```

**Hermes Agent**(`~/.hermes/config.yaml`):

```yaml
mcp_servers:
  patent-checker:
    url: http://127.0.0.1:8642/mcp
    headers:
      Authorization: "Bearer ${PATENT_CHECKER_SERVER_TOKEN}"
```

Skill 自体は `SKILL.md` を含むディレクトリです。このリポジトリの `skills/patent-checker/` をエージェントの skills ディレクトリにコピーするだけで、手動での Skill のインストールは完了します。

</details>

### スマートフォンのみの環境とクラウドエージェント

Skill を `.agents/skills/patent-checker/` としてリポジトリにコミットし(`patent-checker install --scope project` を実行してから、そのディレクトリを git に追加します)、クラウドエージェントが HTTPS で到達できる場所でサーバを動かしてください。ループバックの外にバインドし、`PATENT_CHECKER_SERVER_ALLOWED_HOSTS` にクライアントが使うホスト名を設定し、その前段のリバースプロキシで TLS を終端します。同意の記録はユーザーごとです。ホームディレクトリがリセットされるクラウドエージェントは次回の実行時に再度記録するか、`patent-checker consent record --project` でプロジェクトと一緒に保持してください。

## 使い方

エージェントに、作業中のプロジェクトについての先行特許の探索を依頼します。

> patent-checker skill を使って、このプロジェクトに関連する先行特許を探索してください。

Skill はまず同意の記録を確認し、(プロジェクトごとに一度)`.patent-checker/` を `.gitignore` に追加するかどうかを尋ね、その後、上記の手順を進めて、レポートを `.patent-checker/reports/report-<target>-<YYYYMMDD-HHMM>.md` に書き出します。レポートが上書きされることはありません。同じプロジェクトで後日実行すると、新しい日付付きのファイルが作られます。セッションの間はサーバを起動したままにしてください。

**規模とコスト。** 中規模のプロジェクトの探索 1 回には、コードの読み込み、1 回以上の検索ラウンド、数十件の候補文書の段階的スクリーニングが含まれます。エージェントのトラフィックとして 100 万トークン程度を消費すると見込んでください。その大半はスクリーニングです。エージェントが委譲に対応している場合、Skill は最初のスクリーニング段階をより小さなモデルに任せます。1 回の実行は網羅的ではありません。繰り返しの実行、異なるクエリ語彙、専門家による調査は、それぞれ 1 回の実行では見つからないものを見つけます。

## 設定

サーバ(`patent-checker serve`)と、注記のある場合は CLI が読む環境変数です。コメント付きの同じ一覧が `.env.example` にあります。

| 変数 | 既定値 | 意味 |
|---|---|---|
| `PATENT_CHECKER_OPS_KEY`、`PATENT_CHECKER_OPS_SECRET` | 未設定 | EPO OPS のコンシューマキーとシークレット。値そのものか、`_FILE` 版(`PATENT_CHECKER_OPS_KEY_FILE` など)でそれを保持するファイルのパスを指定します。両方は指定できません。空のファイルは「未設定」を意味します(縮退モード)。 |
| `PATENT_CHECKER_OPERATOR_CONSENT` | 未設定(必須) | 同意した運用者向け告知のバージョン(`1.0`)。未設定の場合、サーバは起動を拒否します。 |
| `PATENT_CHECKER_SERVER_TOKEN`(`_FILE`) | 未設定(http では必須) | クライアントが提示しなければならないベアラートークン。 |
| `PATENT_CHECKER_SERVER_HOST`、`PATENT_CHECKER_SERVER_PORT` | `127.0.0.1`、`8642` | バインドアドレス。ループバックの外にバインドするには、さらに `PATENT_CHECKER_SERVER_ALLOWED_HOSTS`(クライアントが使うホスト名のコンマ区切り)と前段の TLS が必要です。 |
| `PATENT_CHECKER_SERVER_RPS`、`PATENT_CHECKER_SERVER_BURST` | `5`、`10` | 受け付ける MCP リクエストの上限(暴走したエージェントのループに対する防御。上流へのペース制御は別です)。 |
| `PATENT_CHECKER_DATA_DIR` | ユーザーごとのデータディレクトリ(サーバ)、`./.patent-checker`(CLI) | 検索キャッシュ、リクエストログ、および設定されている場合は共有文書キャッシュ(`<dir>/cache`)。コンテナイメージでは `/data` に設定されています。 |
| `PATENT_CHECKER_CACHE_DIR` | `<per-user data dir>/cache` | 共有文書キャッシュ(書誌情報、クレーム、法的状態、ファミリー、Google Patents のページ)。CLI とサーバの両方が、プロジェクトをまたいで再利用します。 |
| `PATENT_CHECKER_CACHE_TTL` | `biblio=90d,claims=0,legal=7d,family=30d,gp=0,search=1d,searchbib=1d` | 種類ごとの有効期限の上書き(`<n>d`、`<n>h`、`0` = 無期限)。 |
| `PATENT_CHECKER_LOG_LEVEL` | `info` | `debug`、`info`、`warning`、`error` のいずれか。サーバのログはすべて標準エラー出力に出ます。 |

Compose では、資格情報は `secrets/` 配下のファイルから取得され、`compose.yaml` の隣の `.env` が `PATENT_CHECKER_OPERATOR_CONSENT`(と、任意で `PATENT_CHECKER_IMAGE`、`PATENT_CHECKER_PORT`、`PATENT_CHECKER_LOG_LEVEL`)を提供します。その他の変数は `compose.yaml` の `environment:` の下に追加できます。

<details>
<summary>コンテナと CLI の間で文書キャッシュを共有する(上級者向け)</summary>

あなたのマシン上の CLI とコンテナ内のサーバは、それぞれ別の文書キャッシュを持ちます。1 つを共有するには、両方を同じディレクトリに向けます。`environment:` の下に `PATENT_CHECKER_CACHE_DIR: /cache` を追加し、`volumes:` の下であなたのキャッシュディレクトリ(たとえば `~/.local/share/patent-checker/cache:/cache`)をバインドマウントします。コンテナは UID 1000 で書き込むので、それがあなたのユーザーでない場合は、マウントしたディレクトリがその UID で書き込み可能である必要があります。

</details>

## キャッシュとクリーンアップ

取得した特許文書は、ユーザーごとの共有キャッシュに一度だけ保存され、種類ごとの有効期限を持ちます。検索結果とリクエストログは、実行した側に残ります(サーバのデータディレクトリ、または CLI をプロジェクトから使った場合はプロジェクトの `.patent-checker/`)。

- `patent-checker cache status` は、何がどこにキャッシュされているか、そのうちどれだけが期限切れまたは破損しているかを表示します。
- `patent-checker cache clear [--kind …] [--older-than DAYS] [--pub …] [--expired] [--broken]` は削除対象を一覧表示します。`--yes` を付けると削除します。
- `patent-checker clean` は、プロジェクトの痕跡(検索キャッシュ、リクエストログ、旧レイアウトの残骸)を一覧表示し、`--yes` を付けると削除します。レポート、エージェントの探索ノート、同意の記録は、`--include-artifacts` または `--include-consent` を付けない限り保持されます。`--shared` を付けると、クリーンアップの範囲が共有キャッシュとサーバのデータディレクトリにまで広がります。
- Compose では、`docker compose down -v` でサーバのボリュームが削除され、キャッシュもすべて消えます。

`--yes` なしで削除されることはありません。

## セキュリティモデル

- サーバは既定でループバックインタフェースにバインドし、すべてのリクエストにベアラートークンを要求します。Host ヘッダと Origin ヘッダを検証しており、これがブラウザのページや DNS リバインディングの試みがループバック上のサーバに到達するのを防ぎます。
- 接続先は `ops.epo.org` と `patents.google.com` だけで、書き込み先は自身のデータディレクトリの配下だけです。コード実行のツールは持ちません。
- 受け取るのは検索式と公報番号だけで、それ以外は受け取りません。あなたのコードと文書は、あなたのマシン上で、あなたのエージェントが分析します。
- 受信リクエストにはレート制限があり、上流へのリクエストはペース制御とキャッシュが施されているため、暴走したエージェントがデータソースを叩き続けることはできません。
- `GET /health` はコンテナのヘルスチェックのために認証なしで応答し、状態とバージョンだけを返します。
- 資格情報は環境変数またはファイルから読み込まれ、ログには決して記録されず、起動時のバナーやエラーメッセージにも現れません。
- `--transport stdio` には認証がありません。サーバがあなたの権限で動作することを承知のうえで、ローカルの単一クライアントに限って使ってください。
- LAN やインターネットに向けて提供することは可能ですが、その責任はあなたにあります。トランスポートは平文の HTTP なので、TLS を終端するリバースプロキシを前段に置き、`PATENT_CHECKER_SERVER_ALLOWED_HOSTS` を設定し、トークンがネットワークとあなたの OPS クォータの間にある唯一の防壁であると考えてください。
- Skill は、サーバが返すものすべてを、特許の本文も含めて、分析対象のデータとして扱い、従うべき指示としては扱いません。

## データソースとフェアユース

- **EPO Open Patent Services** は、あなたが登録したアプリケーションに付随する利用条件(週ごとのフェアユースクォータを含む)のもとで使用されます。サーバはリクエストの間隔を空け、OPS のスロットリングヘッダに従いますが、あなたの資格情報を他にいくつのクライアントが共有しているかは知りようがありません。法的状態の情報は OPS から取得され、レポートではこれが正式な値となります。
- **Google Patents** からは文書ページを一度に 1 件ずつ、人がブラウザで読むのと同程度のペースと量で取得し、すべてのページをキャッシュして二度取得しないようにしています。検索エンドポイントは使用しません。この方針は維持してください。Google の `robots.txt` があるパスを許可していることと、その利用規約が一括収集を許可していることは同じではありません。
- 特許文書の著作権は出願人にあります。レポートは議論するクレームの箇所だけを出典とともに引用し、全文を再配布することはありません。キャッシュはあなたのマシンに留まり、レポートの一部にはなりません。

## 補足

- **米国の公報番号は 2026 年に桁数が変わりました。** EPO の DOCDB 表記では、米国の出願公開は 2025 年までが 10 桁(`US2007016547A1`)、2026 年からは 11 桁(`US20260024003A1`)です。Google Patents はすべての年を 11 桁で表記します。ツールは両方の表記を正規化するので、手で番号を入力するときはどちらの形式でも受け付けられます。
- ユーザー通知(バージョン 1)と運用者向け告知(バージョン 1.0)は、それぞれ独自のバージョン番号を持ちます。どちらかが変わると、Skill が再度同意を求めるか、サーバが改めて承認を求めます。

## アップデート

```sh
uv tool upgrade patent-checker && patent-checker install   # ツールと Skill
docker compose pull && docker compose up -d                # コンテナ化したサーバ
```

`patent-checker install` はいつでも再実行できます。Skill のコピーを更新し、各エージェントの設定にあるサーバのエントリをその場で更新します。

## 変更履歴

- **v0.5**(2026-09): 初回公開リリース。Docker イメージ(GHCR、Docker Hub)とファイルベースのシークレットによる Compose デプロイ、8 つのエージェントに対応した `patent-checker install`、ブートストラップスクリプト、PyPI パッケージ、`/health`、ログレベル設定、作業ディレクトリからの `.env` の探索、この README。
- **v0.4**(2026-09-05): 種類ごとの有効期限と自己修復を備えたユーザーごとの共有文書キャッシュ、`cache status` / `cache clear` / `clean`、2026 年の米国番号の変更に対応した公報番号の正規化。
- **v0.3**(2026-09-04): MCP サーバ(FastMCP 4、Streamable HTTP、ベアラートークン、Host/Origin 検証、外向き接続の許可リスト、レート制限)、運用者向け告知、最初のファイルキャッシュ。
- **v0.2**(2026-08-29): コアライブラリと CLI、同意ゲートとレポートテンプレートを備えた Agent Skill、2 つのプロジェクトでのエンドツーエンド検証。
- **v0.1**(2026-08-29): 手動で実行した概念実証。データソースと法的リスクの調査。

## ライセンス

Apache-2.0。[LICENSE](LICENSE) を参照してください。

質問やバグ報告: [GitHub Issues](https://github.com/xhighhongo41/patent-checker/issues)。
Patent Checker は現状有姿で提供され、いかなる種類の保証もありません。侵害の有無を判定するものではなく、法的助言でもありません。
