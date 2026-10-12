# ローカル実験環境（blossoML ＋ blossoml-ops）

本番と同じ構成を Docker の 1 コンテナで再現します。

- Ubuntu 24.04 で systemd が動き、`blossoml` サービスとして blossoML（設計時点のコミット `64b236c` に 0640 パッチを当ててビルドしたもの）が動きます。
- エージェントは CI と同じ手順で作った conda 環境（Python・Spot・本体）を、`deploy/remote-install.sh` でそのまま配備します。
- ユーザー、ACL、tinyproxy、nftables、systemd ユニットは `blossoml_ops/deploy/README.md` の手順どおりに設定されます（`provision.sh`）。
- 起動時に、blossoML の実際のアップロード API でテスト用 blob とサンプルデータを入れます（`seed.py`）。
  わざと「24 時間以上前の tmp_* の残骸」と「DB にないファイル」を 1 つずつ置くので、daily の点検は warning を出すはずです。

## 準備

```sh
cd local
printf '%s' 'sk-ant-...' > secrets/anthropic_api_key     # Anthropic API キー
chmod 600 secrets/anthropic_api_key
```

bot の Nostr 鍵は、`secrets/nostr_bot_nsec` がなければ初回起動時に自動で作られ、以後も同じものが使われます。
`secrets/` の中身は git に入りません。

## 起動

```sh
docker compose up -d --build            # 初回のビルドは blossoML の依存のコンパイルで 10〜20 分ほど
docker compose logs -f lab | grep lab-init   # 「ready」と bot の npub が出たら準備完了
```

## エージェントを動かす

```sh
docker compose exec lab systemctl start blossoml-ops@daily       # 定期点検（終わるまで戻らない）
docker compose exec lab systemctl start blossoml-ops@postdeploy  # デプロイ後の確認（テスト用 blob を使う）
docker compose exec lab /opt/blossoml-ops/bin/run-manual "直近 1 時間のアップロードの状況を調べて"

docker compose exec lab journalctl -u blossoml-ops@daily -o cat --no-pager   # 実行結果（終了コードと費用）
docker compose exec lab bash -c 'cat /var/lib/blossoml-ops/traces/*.jsonl' | less   # トレース
```

**DM は本物が送られます。** 宛先は設計書の運営者の npub（固定）、送信元はこの環境の bot の鍵です。

## 確かめられること

```sh
docker compose exec lab bash
# I1: エージェントのユーザーは blob を開けない
F=$(ls /home/ubuntu/blossoML/data | grep -E '^[0-9a-f]{64}$' | head -1)
sudo -u blossoml-ops /opt/blossoml-ops/current/env/bin/python -c "open('/home/ubuntu/blossoML/data/$F','rb')"   # PermissionError
# I4: 許可リスト外へは出られない
sudo -u blossoml-ops curl -sS -m 5 https://example.com/                                   # 失敗
sudo -u blossoml-ops curl -sS -m 5 -x http://127.0.0.1:8888 https://example.com/          # 403
sudo -u blossoml-ops curl -sS -m 5 -x http://127.0.0.1:8888 -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/
# 障害を起こしてみる
systemctl stop blossoml && systemctl start blossoml-ops@daily                             # critical になるはず
```

blossoML にはホストから `http://localhost:8082/` でもアクセスできます。

## 片付け

```sh
docker compose down          # データは消えます（次回の起動で入れ直し）
```

## 本番との違い

- コンテナなので、ホストのカーネルや OOM の挙動は本番と同じではありません（`MemoryMax` などのユニット設定自体は効きます）。
- blossoML の systemd ユニットは本番のものがリポジトリにないため、`local/blossoml.service` で代用しています。
- 日次タイマーは有効にしていません（手で起動します）。
