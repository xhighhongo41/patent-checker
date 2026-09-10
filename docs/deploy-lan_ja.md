# Patent Checker を LAN / VPN で提供する(TLS)

English: [deploy-lan.md](deploy-lan.md)

既定のデプロイ(リポジトリ直下の `compose.yaml`)は、サーバーをホストのループバックアドレスにだけ公開します。1 台のマシンで 1 人の開発者が使うにはそれが正解です。複数のマシンやチームで 1 つのサーバーを共有するには、サーバーがネットワーク越しに到達できる必要があり、ループバックでは関係なかった 2 点が重要になります。

- **TLS。** MCP のトランスポートは素の HTTP で、すべてのリクエストがベアラートークンを運びます。ネットワーク上ではトークンが平文で流れるため、TLS を終端するリバースプロキシをサーバーの前に置く必要があります。
- **ホスト名。** サーバーは `Host` ヘッダが許可リストに無いリクエストを拒否します(DNS リバインディング対策)。そのため、クライアントが使う名前を宣言します。

`examples/lan-tls/` は、この 2 つを [Caddy](https://caddyserver.com/) をプロキシとして行う Compose ファイル一式です。サーバーのポートはホストに公開されず、Caddy の 443 だけが公開されます。

## 必要なもの

- すべてのクライアントからこのマシンに解決するホスト名。LAN の DNS、VPN の名前、または各クライアントの hosts ファイルの項目。以下では `patents.lan` と呼びます。
- ループバック構成と同じ 3 つのシークレットファイル(リポジトリ直下の `secrets/README.md` に作り方があります)を `examples/lan-tls/secrets/` に置いたもの。
- Docker Compose v2。

## 手順

```sh
git clone https://github.com/xhighhongo41/patent-checker.git
cd patent-checker/examples/lan-tls
cp .env.example .env
# .env を編集: PATENT_CHECKER_LAN_HOST=patents.lan と運用者告知への同意
# secrets/ops_key.txt、secrets/ops_secret.txt、secrets/server_token.txt を作成
docker compose up -d --wait
```

`docker compose ps` で両方のサービスが healthy と表示されます。Caddy は `https://patents.lan/` で応答し、MCP エンドポイントは `https://patents.lan/mcp`、認証不要のヘルスチェックは `https://patents.lan/health` です。

## 証明書: 内部 CA か ACME か

例の `Caddyfile` には `tls internal` が書かれています。Caddy は初回起動時に自前の認証局を作り、`patents.lan` の証明書をそこから発行します。マシンの外には何も出ず、公開 DNS も不要ですが、**各クライアントがその CA を一度信頼する必要があります**。動作中のコンテナからルート証明書を取り出します。

```sh
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
```

取り出した `caddy-root.crt` を各クライアントの OS の信頼ストアに入れます(macOS: キーチェーンアクセスで「常に信頼」、Windows: `certmgr.msc` の「信頼されたルート証明機関」、Linux: `update-ca-certificates` または `trust anchor`)。コーディングエージェントはランタイム経由で OS のストアを使うので通常はこれで足ります。独自の CA リストを同梱するエージェントには固有の設定が要ることがあり、`curl --cacert caddy-root.crt https://patents.lan/health` が「証明書の問題か、エージェントの問題か」を切り分ける最短の方法です。

ホスト名が**公開 DNS 名**で、このマシンの 80 番と 443 番にインターネットから到達できるなら、代わりに `tls internal` の行を削除します。Caddy が Let's Encrypt から自動的に証明書を取得し、クライアント側の信頼作業は不要になります。

## エージェントの接続

README と同じ手順で、HTTPS の URL を使って登録します。

```sh
TOKEN=$(cat examples/lan-tls/secrets/server_token.txt)
patent-checker install --url https://patents.lan/mcp --token-file examples/lan-tls/secrets/server_token.txt
```

すべてのクライアントが同じベアラートークンを提示します。トークンはサーバーごとに 1 つで、利用者ごとではありません。ローテーションは `server_token.txt` を書き換えて `docker compose up -d --force-recreate patent-checker` を実行し、各クライアントで `patent-checker install` をやり直します。

## 露出を絞る

- マシンにインターネット向けのインターフェースもある場合は、`.env` の `PATENT_CHECKER_LAN_BIND` で Caddy を 1 つのインターフェース(たとえば VPN のアドレス)に限定します。
- サーバーは `PATENT_CHECKER_SERVER_ALLOWED_HOSTS` にある名前しか受け付けません。例では `PATENT_CHECKER_LAN_HOST` から設定しています。ワイルドカードは意図的に拒否されるので、名前はカンマ区切りですべて列挙します。
- 運用者告知はサーバーを立てる人に適用されます。README の告知をチームに周知するのは運用者の責任です。

## すべてを取り除く

```sh
docker compose down -v      # 両方のサービスを止め、データと証明書のボリュームを削除
```

検索キャッシュ、リクエストログ、Caddy の CA は名前付きボリュームにあり、`-v` で消えます。シークレットファイルと `.env` は削除するまでディスクに残ります。
