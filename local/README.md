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
API キーを後から置いた場合は `docker compose exec lab systemctl restart lab-init` で反映されます。
`secrets/` の中身は git に入りません。

## 起動

```sh
docker compose up -d --build            # 初回のビルドは blossoML の依存のコンパイルで 10〜20 分ほど
docker compose exec lab journalctl -u lab-init -o cat --no-pager | grep '\[lab-init\]'   # 「ready」と bot の npub が出たら準備完了
```

## エージェントを動かす

`lab.sh` に操作をまとめてあります（どのディレクトリからでも `local/lab.sh <コマンド>` で使えます）。

```sh
./lab.sh run                 # 定期点検（daily）。終わるまで待ち、最後に終了コードと費用を表示
./lab.sh run postdeploy      # デプロイ後の確認（テスト用 blob を使う）
./lab.sh manual "直近 1 時間のアップロードの状況を調べて"
```

**DM は本物が送られます。** 宛先は設計書の運営者の npub（固定）、送信元はこの環境の bot の鍵です（`./lab.sh init` で npub を表示）。
1 回の実行で 0.1〜0.2 ドル程度かかります（上限は設定の `max_budget_usd`）。

## ログを見る

| コマンド | 見られるもの |
|---|---|
| `./lab.sh trace` | 直前の実行を 1 手ずつ（どのツールを何の引数で呼び、何が返ったか、拒否、終了時のルールの状態）と、送った通知の本文。`./lab.sh trace <run id>` で過去の実行も見られる |
| `./lab.sh result` | 直前の実行の終了コード・ターン数・費用（`./lab.sh result postdeploy` なども可） |
| `./lab.sh follow` | 実行中のエージェント・blossoML・プロキシのログをリアルタイムで（別のターミナルで開いてから `run` すると見やすい。Ctrl-C で終了） |
| `./lab.sh blossoml` | blossoML 自身のログ（`read_logs` が集計している元データ） |
| `./lab.sh raw` | トレースの JSONL そのもの |
| `./lab.sh undelivered` | どのリレーにも届かなかった通知 |
| `./lab.sh init` | 実験環境の準備ログ（テストデータ、bot の npub） |
| `./lab.sh shell` | コンテナの中の root シェル |

終了コード: 0 正常 / 1 内部エラー / 2 ルール未達 / 3 報告が 1 通も届かなかった / 4 ハーネスが異常を検知（critical DM 済み）/ 5 設定・ルールの不整合。

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
