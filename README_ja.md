# Patent Checker

<!-- mcp-name: io.github.xhighhongo41/patent-checker -->

English: [README.md](README.md)

[![PyPI](https://img.shields.io/pypi/v/patent-checker)](https://pypi.org/project/patent-checker/)
[![CI](https://github.com/xhighhongo41/patent-checker/actions/workflows/ci.yml/badge.svg)](https://github.com/xhighhongo41/patent-checker/actions/workflows/ci.yml)
[![Docker Hub](https://img.shields.io/docker/pulls/xhighhongo41/patent-checker)](https://hub.docker.com/r/xhighhongo41/patent-checker)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Patent Checker は、あなた自身のソフトウェアプロジェクトに関係しうる公開特許を探索し、見つけたことを日付入りの手順記録形式のレポートとして書き留めるためのツールです。2 つの部分から成ります。

- **MCP サーバー**: 公開特許データ(EPO Open Patent Services と Google Patents)を決定論的に取得します。検索、書誌、請求項、権利状態、パテントファミリー、およびオフラインの補助ツール。
- **Agent Skill**: あなたの AI コーディングエージェント(Claude Code、Codex CLI、Cursor、Gemini CLI、GitHub Copilot CLI、OpenCode、OpenHands、Hermes Agent)に判断の仕事をさせます。コードベースを読み、その特徴を特許の語彙に翻訳し、候補をふるいにかけ、レポートを書きます。

Patent Checker は**何かが特許を侵害するかどうかを判定しません**。レポートに含まれるのは所見、調査範囲の記述、未解決の問いであり、結論ではありません。

**状態: 安定版(v1.0)。**

## 重要なお知らせ

インストールする前に読んでください。ツールは一度だけ確認を求め、確認したことを記録します。

- **ここで知ったことは、あなたに不利に働きうる。** レポートは、特定の日に特定の特許を知っていたことを記録します。いくつかの法域、特に米国では、特許を知っていたことが故意侵害の主張と損害賠償の増額を支えることがあります。どう対応したかの日付入り記録は、あなたに有利にも働きます。Patent Checker は記録の置き場所とバージョン管理で追跡するかどうかをあなたに選ばせます。意識して選んでください。
- **検索結果は不完全である。** 網羅的な特許検索は存在しません。カバー範囲は国、言語、公開段階、データソースによって異なり、最近の出願はまったく含まれないこともあります。レポートに文書が無いことは、関連特許が存在しないことの証拠には決してなりません。
- **これは検索の補助であり、助言ではない。** Patent Checker は弁理士や特許弁護士の代わりにはならず、作者はその出力に基づいて行われた、あるいは行われなかった判断について責任を負いません。あなたのプロジェクトについての最終判断と、それへの対応は、あなた自身のものです。

お知らせは 2 種類あります。**利用者向け通知**(上の 3 点)は Skill が探索を実行する人に表示し、インストーラも冒頭で表示します。同意はローカルに記録され、毎回の実行前に確認されます。**運用者向け告知**はサーバーを起動する人向けで、データソースの規約、認証情報、サーバーが保存するものを扱い、サーバーは承認されるまで起動を拒否します。

## 仕組み

```
your agent ──(Skill: 判断)──► patent-checker MCP サーバー ──► EPO OPS
   │                              │  決定論的な取得、             Google Patents
   │  コードを読み、               │  正規化、キャッシュ
   ▼  レポートを書く               ▼
.patent-checker/reports/…    利用者単位の文書キャッシュ
```

1. Skill がコードベースを読み、請求されうる技術的特徴を列挙し、特許の語彙に翻訳して検索式を組み立てます。
2. MCP サーバーが EPO OPS に対して検索を実行し、候補文書(請求項は Google Patents、書誌と権利状態は OPS)を取得して、すべての文書をキャッシュします。同じものを二度取得しません。
3. Skill が候補を段階的にふるい、請求項の構成要件をあなたの特徴に対応づけ、レポートを書きます。何を検索し、何が見つかり、各候補があなたのコードとどう関係し、何が*カバーされていない*かを記します。

サーバーが受け取るのは検索式と公報番号だけです。あなたのソースコードとプロジェクトの説明はマシンの外に出ません。分析はエージェントの中で行われます。

## 前提条件

- **Agent Skill と MCP に対応した AI コーディングエージェント**: Claude Code、OpenAI Codex CLI、Cursor、Gemini CLI、GitHub Copilot CLI、OpenCode、OpenHands、Hermes Agent のいずれか。
- **Python 3.12 以上と [uv](https://docs.astral.sh/uv/)**(コマンドラインツールとインストーラ用。`pipx` や仮想環境内の `pip install` でも構いません)。後述のベアラートークンを生成するのにも Python が最も手軽です。
- **Docker と Compose v2**(サーバーをコンテナで動かす場合。推奨)。Docker が無くてもサーバーは通常のプロセスとして動きます。
- **EPO Open Patent Services のアカウント**(推奨)。<https://developers.epo.org/> で登録し、アプリを作成してコンシューマキーとシークレットを取得します。**早めに申請してください。承認は手動で、通常 1 営業日ほどかかります。** 無い場合、サーバーは[縮退モード](#epo-ops-アカウントが無い場合縮退モード)で動きます。

## ベアラートークン

サーバーは HTTP で待ち受け、すべての MCP クライアントは毎回のリクエストで**ベアラートークン**を提示しなければなりません。あなたのサーバーとあなたのエージェントだけが知る秘密の文字列です。同じマシン上の他のプログラム、あるいはポートを外に出した場合はネットワーク上の誰かが、あなたのサーバーと EPO OPS の割り当てを使うのを防ぐものです。アカウント登録のようなものはありません。トークンは自分で一度だけ作り、同じ値をサーバーとインストーラに渡します。

生成方法(ランダムな 32 バイトなら何でも構いません):

```sh
python3 -c "import secrets; print(secrets.token_urlsafe(32))"      # macOS, Linux
```

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"       # Windows
```

置き場所:

- **Docker Compose**: `compose.yaml` と同じ場所の `secrets/server_token.txt`。
- **Docker なし**: `.env` の `PATENT_CHECKER_SERVER_TOKEN=…`(または `PATENT_CHECKER_SERVER_TOKEN_FILE` で指すファイル)。
- **エージェント側**: `patent-checker install` が尋ねる(または `--token-file` から読む)ので、各エージェントの MCP 設定に書き込まれます。

トークンが無いと HTTP サーバーは起動しません。唯一の例外は `patent-checker serve --transport stdio` で、これはエージェントがサーバーを子プロセスとして起動し、あなた自身の権限で動くためトークンがありません。単一のローカルクライアント向けの特殊な形で、既定ではありません。トークンはパスワードと同じように扱ってください。保持するファイルは他人が読めないようにし、ローテーションは新しい値を書いて `patent-checker install` をやり直します。

## EPO OPS アカウントが無い場合(縮退モード)

OPS の認証情報を設定しない(`secrets/ops_key.txt` と `ops_secret.txt` が空、または `.env` に `PATENT_CHECKER_OPS_KEY` が無い)と、サーバーは**縮退モード**で起動し、起動時と `server_status` でそう表示します。

- 動くもの: エージェントが公報番号を既に知っている文書の請求項を Google Patents から取得すること、公報番号の正規化、オフラインの補助ツール(重複排除、バッチ照合)。
- 使えないもの: 特許検索、書誌、権利状態、パテントファミリー。Skill は縮退手順に切り替わり、エージェント自身の Web 検索で候補の公報番号を探します。カバー範囲は下がり、レポートにもそう書かれます。

縮退モードは最初の一瞥のためのものです。本格的な探索には OPS アカウントを取得し、認証情報を置いてサーバーを再起動してください。

## サーバーのインストール

どちらの方法でも、運用者向け告知への承認とベアラートークンが必要です。

### 方法 A: Docker Compose(推奨)

```sh
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/compose.yaml
mkdir secrets

# 1. EPO OPS の認証情報(縮退モードなら両方とも空ファイルのまま)
printf '%s' 'YOUR_OPS_CONSUMER_KEY'    > secrets/ops_key.txt
printf '%s' 'YOUR_OPS_CONSUMER_SECRET' > secrets/ops_secret.txt

# 2. ベアラートークン(前節)
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/server_token.txt
chmod 600 secrets/*.txt

# 3. 運用者向け告知を読み(日本語は --lang ja)、承認する
docker compose run --rm patent-checker serve --show-operator-notice --lang ja
echo 'PATENT_CHECKER_OPERATOR_CONSENT=1.0' >> .env

# 4. 起動
docker compose up -d
curl -fsS http://127.0.0.1:8642/health
```

Windows では `printf` の代わりに PowerShell で 3 つのファイルを作ります(`Set-Content` はバイトオーダーマークを書き込み、それがトークンの一部になってしまうため .NET を直接使います)。

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
New-Item -ItemType File -Force secrets\ops_key.txt, secrets\ops_secret.txt | Out-Null   # 空: 縮退モード
$token = python -c "import secrets; print(secrets.token_urlsafe(32))"
[IO.File]::WriteAllText("$PWD\secrets\server_token.txt", $token)
```

イメージは `ghcr.io/xhighhongo41/patent-checker` と `docker.io/xhighhongo41/patent-checker` に、タグ `X.Y.Z`、`X.Y`、`latest` で公開されています。コンテナ内部では `0.0.0.0` にバインドしますが、Compose はポートを**あなたのマシンのループバックにだけ**公開します(`127.0.0.1:8642`)。サーバーが受け付ける Host ヘッダは `localhost` と `127.0.0.1` だけです。取得した文書、検索結果、リクエストログは名前付きボリューム `patent-checker-data` にあります。コンテナは非特権ユーザーとして、読み取り専用のファイルシステムで、Linux のケーパビリティをすべて落として動きます。お使いの Docker エンジンがこれらの設定を受け付けない場合、`compose.yaml` の `# Hardening` 以下の 4 行は、サーバーの動作を変えずに削除できます。

シークレットファイルは起動時に一度だけ読まれます。空の OPS ファイルは「未設定」(縮退モード)を意味します。Linux の Docker Engine を UID 1000 でないアカウントで動かしている場合は、[`secrets/README.md`](secrets/README.md) のファイル所有権の注記を参照してください。

LAN や VPN で提供する(複数のマシンで 1 つのサーバーを使う)場合は [docs/deploy-lan_ja.md](docs/deploy-lan_ja.md) を参照してください。同じコンテナの前に TLS を終端するプロキシを置きます。

### 方法 B: Docker を使わない

```sh
uv tool install patent-checker          # または: pipx install patent-checker
mkdir patent-checker-server && cd patent-checker-server
curl -fsSLO https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/.env.example
cp .env.example .env
```

次に `.env` を編集します。

1. EPO OPS のキーとシークレットを `PATENT_CHECKER_OPS_KEY` と `PATENT_CHECKER_OPS_SECRET` に書く(縮退モードなら空のまま)。
2. ベアラートークンを生成し(前節)、`PATENT_CHECKER_SERVER_TOKEN` に書く。
3. `patent-checker serve --show-operator-notice`(日本語は `--lang ja`)で運用者向け告知を読み、`PATENT_CHECKER_OPERATOR_CONSENT=1.0` を設定する。

最小の `.env` は次のようになります。

```sh
PATENT_CHECKER_OPS_KEY=YOUR_OPS_CONSUMER_KEY
PATENT_CHECKER_OPS_SECRET=YOUR_OPS_CONSUMER_SECRET
PATENT_CHECKER_SERVER_TOKEN=the-token-you-generated
PATENT_CHECKER_OPERATOR_CONSENT=1.0
```

`patent-checker serve` でサーバーを起動します。`http://127.0.0.1:8642/mcp` で Streamable HTTP を話します。`.env` は起動したディレクトリとその親から探されますが、ホームディレクトリとファイルシステムのルートは対象外です。既に環境変数にある値が優先されます。データは利用者単位のデータディレクトリ(Linux と macOS は `~/.local/share/patent-checker`、Windows は `%LOCALAPPDATA%\patent-checker`)に置かれます。ポートが使用中ならサーバーはメッセージを出して停止します。`--port` で別のポートを選び、その URL をエージェントに登録してください。

`--transport stdio` はネットワークポートもトークンも使わずにサーバーを動かします。単一のローカルクライアント専用で、あなたのユーザー権限で動くため、既定は HTTP です。エージェントへの登録方法は [docs/mcp-clients_ja.md](docs/mcp-clients_ja.md) にあります。

## Skill のインストールとエージェントの接続

1 つのコマンドで、コマンドラインツールをインストールし、利用者向け通知を表示し、エージェントが読む場所に Skill をコピーし、MCP サーバーを登録します。

```sh
curl -LsSf https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.sh | sh
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/xhighhongo41/patent-checker/main/install.ps1 | iex"
```

先にスクリプトを読みたい場合は `| sh` の代わりに `| more` で取得するか、スクリプトを使わずに済ませることもできます。スクリプトがするのは uv とツールのインストールだけで、その後インストーラを自分で実行するよう案内します。

```sh
uv tool install patent-checker
patent-checker install
```

`patent-checker install` は次を行います。

1. 利用者向け通知を表示して同意を求める(`~/.config/patent-checker/consent.json` に日付と通知の版を記録。再び尋ねられるのは通知が変わったときだけ)。
2. ベアラートークンを尋ねる(または `--token-file` から読む)。
3. Skill を `~/.agents/skills/patent-checker/`(Codex CLI、OpenCode、Cursor、Gemini CLI、Copilot CLI が読む)と、専用ディレクトリが必要なエージェントの場所(`~/.claude/skills/`、`~/.openhands/skills/`、`~/.hermes/skills/`)にコピーする。
4. 検出した各エージェントにサーバー(`--url` を渡さなければ `http://127.0.0.1:8642/mcp`)を登録する。エージェント自身の `mcp add` コマンドがあればそれを使い(Claude Code、Gemini CLI、Copilot CLI、`--token-env` 指定時の Codex CLI)、無ければエージェントの JSON または TOML 設定を編集し(Cursor、OpenCode、Codex CLI)、設定に触れないエージェント(OpenHands、Hermes Agent)には貼り付け用のスニペットを表示する。
5. エージェントごとに何をしたかの表を表示する。いずれかのステップが失敗すると終了コードは非ゼロになります。スニペットの表示は失敗ではありません。

| エージェント | 登録方法 | 設定ファイル |
|---|---|---|
| Claude Code | `claude mcp add` | ユーザー設定(`--scope project` なら `.mcp.json`) |
| Gemini CLI | `gemini mcp add` | `~/.gemini/settings.json` |
| GitHub Copilot CLI | `copilot mcp add` | `~/.copilot/mcp-config.json` |
| Cursor | ファイルを直接編集 | `~/.cursor/mcp.json` |
| OpenCode | ファイルを直接編集 | `~/.config/opencode/opencode.json` |
| Codex CLI | `codex mcp add` / 追記 | `~/.codex/config.toml` |
| OpenHands、Hermes Agent | 貼り付け用スニペット | `config.toml`、`~/.hermes/config.yaml` |

便利なオプション: `--list-agents`(何がどこにインストールされるか)、`--dry-run`(書き込み以外をすべて行う)、`--agent claude-code --agent cursor`(自動検出の代わりに指定。`--agent all` で対応する全エージェント)、`--scope project`(ホームディレクトリではなく現在のプロジェクトにインストール)、`--lang ja`(日本語の通知)、`--no-skill` / `--no-mcp`。インストーラの再実行は安全です。Skill のコピーを更新し、サーバーの項目をその場で更新し、初回に作った元の設定ファイルのバックアップ(`.bak`)は決して上書きしません。唯一の例外は Codex CLI で、既存の項目はそのまま残ります。変更するには `~/.codex/config.toml` を編集してください。

### ベアラートークンを露出させずに渡す

インストーラはトークンをコマンドライン引数として受け取らないので、シェルの履歴に残りません。次のいずれかで渡します。

```sh
patent-checker install --token-file /path/to/secrets/server_token.txt
# または
export PATENT_CHECKER_SERVER_TOKEN="$(cat /path/to/secrets/server_token.txt)"
patent-checker install
# または、そのまま実行する: インストーラがエコーなしでトークンを尋ねます
```

既定では、トークンの値は各エージェント自身の設定ファイルに書き込まれ、そのファイルはあなただけが読めるようにされます(Windows ではファイルの通常の権限のままです)。`--token-env` を付けると、インストーラは値をまったく必要としません。環境変数 `PATENT_CHECKER_SERVER_TOKEN` への*参照*を各エージェントが展開する記法で書き、あなたはエージェントの起動前にその変数を export します。Copilot CLI と OpenHands はそのような参照を文書化していないため、`--token-env` ではこれらに対してスニペットを表示します。エージェント自身の CLI で登録する場合、呼び出しの間だけトークンがそのプロセスの引数に現れます。

`--scope project` では、トークンを保持する設定ファイルがプロジェクトディレクトリの中に書かれます。インストーラは注意を促します。コミットしないでください。

インストーラが表示する登録コマンドやスニペットにトークンは含まれません。`${PATENT_CHECKER_SERVER_TOKEN}` かプレースホルダを使います。

### サーバーが 401 を返す場合

401 は、エージェントが送るトークンがサーバー起動時のものと違うという意味です。両者を比べてください。サーバー側は `secrets/server_token.txt` か `.env`、エージェント側は上の表の設定ファイルにあります(Claude Code は `claude mcp get patent-checker` で表示できます)。`<token>` のようなプレースホルダがそのまま登録されていないか確認してください。サーバーが動いていない場合は 401 ではなく*接続拒否*のエラーになるので、両者は簡単に区別できます。

### 手動で登録する

各エージェントの正確なコマンドやファイル、GitHub Copilot coding agent、stdio 形式は [docs/mcp-clients_ja.md](docs/mcp-clients_ja.md) にあります。

### スマートフォンのみ・クラウドエージェント

Skill を `.agents/skills/patent-checker/` としてリポジトリにコミットし(`patent-checker install --scope project --no-mcp` の後、ディレクトリを git に追加)、クラウドエージェントが HTTPS で到達できる場所でサーバーを動かします([docs/deploy-lan_ja.md](docs/deploy-lan_ja.md))。同意記録は利用者単位です。ホームディレクトリがリセットされるクラウドエージェントは次回の実行で再び記録するか、`patent-checker consent record --project` でプロジェクトと一緒に保持します。

## 使い方

エージェントに、作業中のプロジェクトの先行技術探索を頼みます。

> patent-checker スキルを使って、このプロジェクトに関連する先行特許を探索して。

Skill はまず同意記録を確認し、(プロジェクトごとに一度)`.patent-checker/` を `.gitignore` に追加するかを尋ね、上の手順を進めて、レポートを `.patent-checker/reports/report-<target>-<YYYYMMDD-HHMM>.md` に書きます。レポートは上書きされません。同じプロジェクトで後日実行すると、新しい日付入りファイルができます。セッションの間はサーバーを動かしたままにしてください。

**規模とコスト。** 中規模プロジェクトの探索 1 回は、コードの読み取り、1 回以上の検索ラウンド、数十件の候補文書の段階的なふるい分けを伴います。エージェントのトラフィックとして 100 万トークン程度(開発中に Claude Code と中規模プロジェクトで計測)を消費すると見込んでください。大半はふるい分けで、エージェントが委譲に対応していれば Skill は最初の段階を小さなモデルに委ねます。1 回の実行は網羅的ではありません。繰り返しの実行、別の検索語彙、専門家による調査は、それぞれ 1 回の実行では見つからないものを見つけます。

## 設定

サーバー(`patent-checker serve`)と、注記のあるものは CLI も読む環境変数です。コメント付きの同じ一覧が `.env.example` にあります。CLI の `--host` と `--port` オプションは `PATENT_CHECKER_SERVER_HOST` と `PATENT_CHECKER_SERVER_PORT` より優先されます。

| 変数 | 既定 | 意味 |
|---|---|---|
| `PATENT_CHECKER_OPS_KEY`、`PATENT_CHECKER_OPS_SECRET` | 未設定 | EPO OPS のコンシューマキーとシークレット。値そのものか、`_FILE` 変種(`PATENT_CHECKER_OPS_KEY_FILE` など)で値を持つファイルのパスを指定。両方は不可。空ファイルは「未設定」(縮退モード)。 |
| `PATENT_CHECKER_OPERATOR_CONSENT` | 未設定(必須) | 承認した運用者向け告知の版(`1.0`)。無いとサーバーは起動を拒否します。 |
| `PATENT_CHECKER_SERVER_TOKEN`(`_FILE`) | 未設定(http では必須) | クライアントが提示すべきベアラートークン。 |
| `PATENT_CHECKER_SERVER_HOST`、`PATENT_CHECKER_SERVER_PORT` | `127.0.0.1`、`8642` | バインド先。ループバック以外へのバインドには、さらに `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` と前段の TLS が必要です。 |
| `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` | 未設定 | クライアントが Host ヘッダで使うホスト名のカンマ区切り。名前のみ。ワイルドカードは拒否され、IPv6 アドレスは角括弧なしで書きます。 |
| `PATENT_CHECKER_SERVER_RPS`、`PATENT_CHECKER_SERVER_BURST` | `5`、`10` | 受け付ける MCP リクエストの上限(暴走したエージェントのループへの備え。上流の間隔制御は別)。 |
| `PATENT_CHECKER_DATA_DIR` | 利用者単位のデータディレクトリ(サーバー)、`./.patent-checker`(CLI) | 検索キャッシュ、リクエストログ、設定時は共有文書キャッシュ(`<dir>/cache`)。サーバーはそれを動かす利用者のもとに、CLI は実行したプロジェクトのもとにデータを置きます。コンテナイメージは `/data` に設定しています。 |
| `PATENT_CHECKER_CACHE_DIR` | `<利用者単位データディレクトリ>/cache` | 共有文書キャッシュ(書誌、請求項、権利状態、ファミリー、Google Patents のページ)。プロジェクトをまたいで CLI とサーバーが共通に再利用します。 |
| `PATENT_CHECKER_CACHE_TTL` | `biblio=90d,claims=0,legal=7d,family=30d,gp=0,search=1d,searchbib=1d` | 種類ごとの有効期限の上書き。`<n>d`、`<n>h`、または `0` = **無期限**(「キャッシュしない」ではありません)。 |
| `PATENT_CHECKER_LOG_LEVEL` | `info` | `debug`、`info`、`warning`、`error`。サーバーのログはすべて標準エラーに出ます。 |

`.env` はすべての `patent-checker` コマンドが、実行ディレクトリかその親から読みます。ホームディレクトリとファイルシステムのルートからは読みません。Compose では認証情報は `secrets/` のファイルから来て、`compose.yaml` と同じ場所の `.env` が `PATENT_CHECKER_OPERATOR_CONSENT`(任意で `PATENT_CHECKER_IMAGE`、`PATENT_CHECKER_PORT`、`PATENT_CHECKER_LOG_LEVEL`)を提供します。他の変数は `compose.yaml` の `environment:` に追加できます。

<details>
<summary>コンテナと CLI で文書キャッシュを共有する(上級者向け)</summary>

あなたのマシンの CLI とコンテナ内のサーバーは別々の文書キャッシュを持ちます。1 つを共有するには、両方を同じディレクトリに向けます。`environment:` に `PATENT_CHECKER_CACHE_DIR: /cache` を追加し、`volumes:` であなたのキャッシュディレクトリ(例: `~/.local/share/patent-checker/cache:/cache`)をバインドマウントします。コンテナは UID 1000 として書き込むので、それがあなたのユーザーでなければ、マウントしたディレクトリはその UID が書けるようにする必要があります。コンテナの中で `patent-checker clean --shared` は実行しないでください。コンテナのデータを消すにはボリュームを削除します(`docker compose down -v`)。

</details>

## キャッシュと後始末

取得した特許文書は利用者単位の共有キャッシュに 1 つだけ、種類ごとの有効期限付きで保持されます。検索結果とリクエストログは実行した側に残ります(サーバーのデータディレクトリ、または CLI をプロジェクトから使った場合はそのプロジェクトの `.patent-checker/`)。

- `patent-checker cache status` は、何がどこにキャッシュされ、どれだけが期限切れか壊れているかを表示します。
- `patent-checker cache clear [--kind KIND] [--older-than DAYS] [--pub …] [--expired] [--broken]` は削除対象を一覧し、`--yes` を付けると削除します。`KIND` は `biblio`、`claims`、`legal`、`family`、`gp`、`search`、`searchbib` のいずれかです。
- `patent-checker clean` はプロジェクトの痕跡(検索キャッシュ、リクエストログ、古い配置の残骸)を一覧し、`--yes` で削除します。レポート、エージェントの探索メモ、同意記録は `--include-artifacts` や `--include-consent` を付けない限り残されます。`--shared` は共有キャッシュとサーバーのデータディレクトリまで後始末の範囲を広げます。
- Compose では `docker compose down -v` がサーバーのボリュームを、キャッシュごと削除します。

`--yes` なしでは何も削除されません。

## セキュリティモデル

- サーバーは既定でループバックインターフェースにバインドし、すべてのリクエストにベアラートークンを要求し、定数時間で比較します。Host と Origin ヘッダは設定した名前(ワイルドカード不可)と照合され、DNS リバインディングの試みがループバックのサーバーに届くのを防ぎます。ループバック自身から提供されたページはこの検査では止まりませんが、トークンで締め出されます。
- 接続先は `ops.epo.org` と `patents.google.com` だけで、サーバーでもコマンドラインツールでも同じです。書き込みは自身のデータディレクトリの下だけです。コード実行ツールはありません。
- 受け取るのは検索式と公報番号だけで、それ以外は受け取らず、大きすぎるリクエストは拒否します。あなたのコードと文書は、あなたのマシン上のエージェントが分析します。認証済みのクライアントはサーバーがデータを置く場所を見ることができます(`server_status`)が、ホストについてそれ以外は露出しません。
- 受け付けるリクエストにはレート制限があり、上流へのリクエストは間隔を空け、1 つずつ直列に実行し、キャッシュするので、暴走したエージェントがデータソースを叩き続けることはありません。
- `GET /health` はコンテナのヘルスチェック用に認証不要で、状態と版だけを返します。
- 認証情報は環境変数かファイルから読まれ、ログに出ず、起動バナーやエラーメッセージにも現れません。
- コンテナは非特権ユーザーとして、読み取り専用のファイルシステムで、ケーパビリティをすべて落として動きます。書けるのは `/data` だけです。
- `--transport stdio` には認証がありません。サーバーがあなたの権限で動くことを承知のうえ、単一のローカルクライアントにだけ使ってください。
- LAN やインターネットへの提供は可能ですが、あなたの責任です。トランスポートは素の HTTP なので、TLS を終端するリバースプロキシを前に置き([docs/deploy-lan_ja.md](docs/deploy-lan_ja.md))、`PATENT_CHECKER_SERVER_ALLOWED_HOSTS` を設定し、トークンをネットワークとあなたの OPS 割り当ての間にある唯一のものとして扱ってください。
- Skill はサーバーが返すもの(特許本文を含む)を、従うべき指示ではなく分析対象のデータとして扱います。
- リリースはバージョンタグから CI がビルド・公開し、依存パッケージとコンテナイメージは変更のたびに既知の脆弱性をスキャンします。セキュリティ上の問題の報告は [SECURITY.md](SECURITY.md) を参照してください。

## データソースとフェアユース

- **EPO Open Patent Services** は、登録したアプリケーションに付随する規約(週あたりのフェアユース割り当てを含む)のもとで使われます。サーバーはリクエストの間隔を空け、OPS のスロットリングヘッダに従いますが、同じ認証情報を他に何クライアントが共有しているかは知りようがありません。権利状態の情報は OPS から取得し、レポートではそれが権威ある値です。
- **Google Patents** は文書ページを 1 つずつ、人がブラウザで読むのと同程度のペースと量で取得し、すべてのページをキャッシュして二度取得しません。検索エンドポイントは意図的に使いません。Google の `robots.txt` があるパスを許可していることは、利用規約が大量収集を許可していることと同じではないからです。
- 特許文書は出願人が著作権を持ちます。レポートは扱う請求項の箇所だけを出典付きで引用し、全文を再配布することはありません。キャッシュはあなたのマシンに留まり、レポートの一部にはなりません。

## 補足

- **米国の公開番号は 2026 年に桁数が変わりました。** EPO の DOCDB 表記では、米国の出願公開は 2025 年まで 10 桁(`US2007016547A1`)、2026 年から 11 桁(`US20260024003A1`)です。Google Patents は全年代を 11 桁で綴ります。ツールは両方の綴りを正規化するので、手で番号を入力するときはどちらの形でも受け付けられます。
- 権利状態がイベント 0 件で返ってきた文書はそのように報告されます(`events: []` と注記)。「見つからない」とは別物です。
- 通知が変わると、Skill は再び同意を求め、サーバーは新たな承認を求めます。それまでは何も二度尋ねられません。

## 更新

**Docker Compose**: `docker compose pull && docker compose up -d`。

**Docker なし**: 動いているサーバーを止めてから、

```sh
uv tool upgrade patent-checker      # または: pipx upgrade patent-checker
patent-checker serve
```

**あなたのマシンのツールと Skill**: `uv tool upgrade patent-checker && patent-checker install`。インストーラの再実行は Skill のコピーと各エージェント設定のサーバー項目を更新します。サーバーと Skill は同じ版に揃えてください。両方が同じコマンドで更新され、新しいリリースの Skill は古いサーバーに無いツールを呼ぶことがあります。

## アンインストール

- **エージェント**: 各エージェントの MCP 設定から `patent-checker` の項目を削除し(`claude mcp remove patent-checker`、`gemini mcp remove patent-checker`、または上の表のファイルから項目を削除)、Skill のディレクトリ `~/.agents/skills/patent-checker/`、`~/.claude/skills/patent-checker/`、`~/.openhands/skills/patent-checker/`、`~/.hermes/skills/patent-checker/`(および `--scope project` を使ったプロジェクトの `.agents/skills/` と `.claude/skills/` の同名ディレクトリ)を削除します。
- **サーバー**: `docker compose down -v`(コンテナ、キャッシュ、すべて)または `uv tool uninstall patent-checker`(キャッシュも消すなら、利用者単位のデータディレクトリ `~/.local/share/patent-checker` または `%LOCALAPPDATA%\patent-checker` も)。
- **記録**: プロジェクト内で `patent-checker clean --yes --include-artifacts --include-consent` を実行すると、そのプロジェクトのレポート、メモ、同意記録が消えます(ツールをアンインストールする前に実行してください)。利用者単位の同意記録は `~/.config/patent-checker/consent.json` にあります。

## 変更履歴

- **v1.0**(2026-09): 安定版。ベアラートークンの定数時間比較、`.env` をホームディレクトリとファイルシステムのルートから読まない、リクエストのサイズ上限、コンテナのハードニング(読み取り専用、ケーパビリティなし)、Caddy による LAN/TLS の構成例、インストーラの改善(`--token-env` はトークンの値を要求しない、設定ファイルは所有者のみ読める、バックアップを上書きしない、失敗時は非ゼロ終了、Codex CLI の登録)、キャッシュとパーサーの修正(並行書き込み、CPC 記号、空の権利状態、非 UTF-8 ページ)、上流呼び出しの 401/429/503 での 1 回の再試行と無関係なツールを待たせない改善、すべてのコマンドが `.env` を読む、トレースバックの代わりに分かりやすいエラーメッセージ、Python 3.13、CI でのセキュリティスキャン、MCP Registry への掲載、文書の再構成。
- **v0.5**(2026-09-06): 最初の一般公開。Docker イメージ(GHCR、Docker Hub)とファイルベースの secrets による Compose デプロイ、8 エージェント向けの `patent-checker install`、ブートストラップスクリプト、PyPI パッケージ、`/health`、ログレベル設定。
- **v0.4**(2026-09-05): 種類ごとの有効期限と自己修復を備えた利用者単位の共有文書キャッシュ、`cache status` / `cache clear` / `clean`、2026 年の米国番号変更に対応した公報番号の正規化。
- **v0.3**(2026-09-04): MCP サーバー(FastMCP 4、Streamable HTTP、ベアラートークン、Host/Origin 検証、外向き許可リスト、レート制限)、運用者向け告知、最初のファイルキャッシュ。
- **v0.1〜v0.2**(2026-08): 開発版。コアライブラリ、CLI、同意ゲートとレポート雛形を備えた Agent Skill。

## ライセンス

Apache-2.0。[LICENSE](LICENSE) を参照してください。

質問とバグ報告: [GitHub Issues](https://github.com/xhighhongo41/patent-checker/issues)。
