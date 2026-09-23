# Patent Checker を 1 台の外で動かす(LAN、VPN、tailnet)

English: [deploy-lan.md](deploy-lan.md)

既定の構成(リポジトリ直下の `compose.yaml`)は、サーバーをホストのループバックアドレスにだけ公開します。開発者 1 人が 1 台で使うならこれが適切です。複数のマシンやチームで 1 つのサーバーを共有するときは、サーバーにネットワーク越しに到達できる必要があります。本書はその方法を 3 つ示し、それぞれで利用者に任される点を説明します。

## サーバーが行うこと・行わないこと

- **サーバーは TLS を提供せず、要求もしません。** MCP のトランスポートは素の HTTP で、すべての要求がベアラートークンを運びます。その通信が暗号化されるかどうかは、サーバーを置くネットワークと、その前に置くものによって決まります。
- **どのネットワークに置くかは運用者の責任です。** tailnet や WireGuard の VPN はメンバー間の通信を暗号化しますが、家庭や職場の LAN は通常暗号化しておらず、通信を読める人はトークンも読めます。使うネットワークがそれに見合うだけ信頼できるかを判断してください。TLS 終端プロキシを用意できるならそれを使ってください。推奨の構成であり、サーバーは手を加えずにその背後で動きます。
- **ホスト名は常に検査されます。** サーバーは、`Host` ヘッダが許可リストにない要求をネットワークによらず拒否します(DNS リバインディング対策)。クライアントが使う名前を `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` で宣言してください。ワイルドカードは意図的に受け付けません。名前をすべてカンマ区切りで並べます。
- **トークンはサーバーに 1 つです。** サーバーに到達でき、トークンを持つ人は誰でも使えます。利用者ごとのアカウントはありません。

| 構成 | 通信路の暗号化 | 証明書 | 向いている場面 |
|---|---|---|---|
| 1. Tailscale Serve | HTTPS(Tailscale が終端) | Tailscale がマシンの tailnet 名で発行 | すでに使っている tailnet |
| 2. Caddy(`examples/lan-tls/`) | HTTPS(Caddy が終端) | Caddy の内部 CA、または公開名なら Let's Encrypt | 公開 DNS の有無によらず LAN・VPN |
| 3. プライベートネットワーク上の素の HTTP | ネットワーク自体が提供するもののみ(例: WireGuard) | なし | プロキシを置きたくない、信頼する VPN・LAN |

## 1. Tailscale Serve

[Tailscale Serve](https://tailscale.com/kb/1312/serve) は、tailnet 内の HTTPS の名前を、マシンの `127.0.0.1` 上のポートへ転送します。サーバーは既定のループバックへのバインドのままで構いません。Serve が転送できるのは `127.0.0.1` だけなので、むしろそれが前提です。

初回の前に、Tailscale の管理コンソールで **MagicDNS** と **HTTPS 証明書**を有効にしてください。マシンの名前は `patents.example-tailnet.ts.net` のような形になります(`tailscale status` で確認できます)。Headscale のような自前のコントロールサーバーで tailnet を運用している場合、Serve とその証明書が使えるかはそのサーバーの対応次第です。構成 3 はどの tailnet でも使えます。

Docker では、README のとおり既定の構成を起動してから次を行います。

```sh
# compose.yaml の隣の .env: tailnet の名前を足す(ループバックの名前は残す)
PATENT_CHECKER_SERVER_ALLOWED_HOSTS=patents.example-tailnet.ts.net,localhost,127.0.0.1

docker compose up -d --wait
tailscale serve --bg 8642
```

Docker を使わない場合は、同じ `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` を `.env` に書いて `patent-checker serve` を起動し、同じ `tailscale serve` を実行します。

Serve のコマンド書式は Tailscale の版によって変わっています。上のコマンドが受け付けられないときは、`tailscale serve --help` で手元の版の書式を確認してください(古い版では `tailscale serve https / http://127.0.0.1:8642` と書きます)。`tailscale serve status` で公開中の内容を確認でき、`tailscale serve --https=443 off`(または `tailscale serve reset`)で止まります。tailnet 内の別のマシンから確認します。

```sh
curl https://patents.example-tailnet.ts.net/health
```

MCP のエンドポイントは `https://patents.example-tailnet.ts.net/mcp` です。この用途に Tailscale **Funnel** は使わないでください。Funnel はサービスをインターネット全体に公開します。

## 2. Caddy で TLS(`examples/lan-tls/`)

`examples/lan-tls/` は、[Caddy](https://caddyserver.com/) を TLS 終端プロキシとして組み込んだ Compose ファイルです。サーバーのポートはホストに公開されず、公開されるのは Caddy の 443 番だけです。

必要なものは、すべてのクライアントからこのマシンに解決されるホスト名(LAN の DNS、VPN の名前、または各クライアントの hosts ファイル。以下では `patents.lan`)、ループバック構成と同じ 3 つの秘密ファイル(作り方はリポジトリ直下の `secrets/README.md`)を `examples/lan-tls/secrets/` に置いたもの、Docker Compose v2 です。

```sh
git clone https://github.com/xhighhongo41/patent-checker.git
cd patent-checker/examples/lan-tls
cp .env.example .env
# .env を編集: PATENT_CHECKER_LAN_HOST=patents.lan と運用者の同意
# secrets/ops_key.txt、secrets/ops_secret.txt、secrets/server_token.txt を作る
docker compose up -d --wait
```

`docker compose ps` で両サービスが healthy になります。Caddy は `https://patents.lan/` で応答し、MCP のエンドポイントは `https://patents.lan/mcp`、認証なしのヘルスチェックは `https://patents.lan/health` です。この例ではサーバーの許可リストを `PATENT_CHECKER_LAN_HOST` から設定します。

### 証明書: 内部 CA か ACME か

例の `Caddyfile` には `tls internal` が入っています。Caddy は初回起動時に自前の認証局を作り、そこから `patents.lan` の証明書を発行します。外部には何も出ず公開 DNS も不要ですが、**各クライアントで一度その CA を信頼させる必要があります**。起動中のコンテナからルート証明書を取り出します。

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

各クライアントで `caddy-root.crt` を OS の信頼ストアに入れます(macOS: キーチェーンアクセスで「常に信頼」、Windows: `certmgr.msc` の「信頼されたルート証明機関」、Linux: `update-ca-certificates` または `trust anchor`)。コーディングエージェントは通常ランタイム経由で OS のストアを使うので、たいていはこれで足ります。独自の CA リストを同梱するエージェントには別の設定が要ることがあります。`curl --cacert caddy-root.crt https://patents.lan/health` を試すと、証明書とエージェントのどちらに問題があるかをすぐ切り分けられます。

ホスト名が**公開 DNS 名**で、マシンの 80 番と 443 番にインターネットから到達できる場合は、代わりに `tls internal` の行を削除してください。Caddy が Let's Encrypt から自動で証明書を取得し、クライアント側の信頼の手順は不要になります。

マシンがインターネットに面したインターフェースも持つときは、`.env` の `PATENT_CHECKER_LAN_BIND` で Caddy を 1 つのインターフェース(例: VPN のアドレス)にだけバインドしてください。

## 3. プライベートネットワーク上の素の HTTP

WireGuard の VPN や信頼する LAN では、サーバーがそのネットワークのアドレスで直接待ち受けることもできます。この場合トークンはそのネットワーク内を暗号化されずに流れ、ネットワークの参加者全員がサーバーに到達できます。参加者と暗号化の状態に納得できるネットワークでだけ使ってください。

Docker を使わない場合は、`patent-checker serve` を起動する場所の `.env` に次を書きます。

```sh
PATENT_CHECKER_SERVER_HOST=10.8.0.1
PATENT_CHECKER_SERVER_ALLOWED_HOSTS=10.8.0.1,patents.vpn
```

Docker では、`compose.yaml` の `ports:` の行の `127.0.0.1:` をそのネットワークのアドレスに置き換え(`"10.8.0.1:${PATENT_CHECKER_PORT:-8642}:8642"`)、`.env` に上と同じ `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` を書きます。

マシンがインターネットに面したインターフェースも持つときは、`0.0.0.0` ではなくプライベートネットワークのアドレスにバインドしてください。起動時の表示で TLS を提供しない旨が出ますが、これは注意書きです。

## エージェントの接続

各クライアントのマシンで CLI を入れ、サーバーの URL(構成 1・2 は `https://…`、構成 3 は `http://10.8.0.1:8642/mcp`)とトークンで登録します。

```sh
uv tool install patent-checker
patent-checker install --url https://patents.example-tailnet.ts.net/mcp --token-file ~/patent-checker-token.txt
```

- **トークンの配り方。** サーバーのトークンファイルを、信頼できる経路(その tailnet や VPN 自体、パスワードマネージャー)で各クライアントにコピーし、本人だけが読めるようにして(macOS と Linux では `chmod 600`)、`--token-file` で指定します。`--token-env` を使うと、参照を展開できるエージェントにはトークンの代わりに `PATENT_CHECKER_SERVER_TOKEN` への参照を書きます(README の「ベアラートークンを露出させずに渡す」を参照)。
- **トークンの入れ替え。** サーバーのトークンファイルを書き換えてサーバーを再起動し(`docker compose up -d --force-recreate patent-checker`)、各クライアントで新しいトークンを使って `patent-checker install` をやり直します。
- **クライアントの削除。** `patent-checker uninstall` でそのマシンの登録と Skill を取り除きます。

`patent-checker install --json` は登録した内容を JSON で出力するので、複数のマシンをスクリプトで設定するときに便利です。

## すべてを取り除く

- 構成 1: `tailscale serve reset` を実行し、README のとおりサーバーを止めます。
- 構成 2: `examples/lan-tls/` で次を実行します。

  ```sh
  docker compose down -v      # 両サービスを止め、データと証明書のボリュームを削除する
  ```

  検索キャッシュ、要求ログ、Caddy の CA は名前付きボリュームにあり、`-v` で消えます。秘密ファイルと `.env` は削除するまでディスクに残ります。
- 構成 3: サーバーを止め、`.env` または `compose.yaml` からアドレスを外します。

運用者告知はサーバーを動かす人に適用されます。README の告知をチームに伝えるのは運用者の責任です。
