# blossoml-ops 本番セットアップ手順

設計書の 7.1〜7.7 に対応する手作業の手順です。root 権限が要るものはすべてここで行い、CI からは変更できません。
コマンドは本番サーバ上で、sudo できるユーザー（ubuntu）で実行します。

## 0. 事前確認

```sh
uname -m                 # x86_64 / aarch64 → CI の PROD_RUNNER を合わせる
# Python と Spot は CI が作る conda 環境に入っているので、本番の Python は使わない
sudo apt-get install -y acl tinyproxy nftables
df -h /opt                # 1 リリースは展開後に約 700MB（Python・Spot・同梱の Claude Code CLI を含む）。直近 3 版を残す
```

## 1. blossoML 側の前提（設計書 8 章）

1. `blossoml-patches/0001-fix-create-blob-and-temp-files-with-mode-0640.patch` を blossoML に PR としてマージし、デプロイする。
2. ユニットのドロップインを入れる（`OOMScoreAdjust=-500`、`UMask=0027`）。

   ```sh
   sudo install -D -m 0644 systemd/blossoml.service.d/override.conf /etc/systemd/system/blossoml.service.d/override.conf
   sudo systemctl daemon-reload && sudo systemctl restart blossoml
   ```

3. 既存の blob と一時ファイルを 0640 にする。

   ```sh
   sudo find /home/ubuntu/blossoML/data -maxdepth 1 -type f -exec chmod 0640 {} +
   ```

## 2. ユーザーと権限（7.1）

```sh
sudo useradd --system --home-dir /var/lib/blossoml-ops --create-home --shell /usr/sbin/nologin blossoml-ops
sudo usermod -aG systemd-journal blossoml-ops      # ubuntu グループには入れない
sudo install -d -o blossoml-ops -g blossoml-ops -m 0750 /var/lib/blossoml-ops /var/lib/blossoml-ops/work

# ACL: 通過・一覧・stat だけ。blob ファイルには何も付けない。デフォルト ACL も付けない。
sudo setfacl -m u:blossoml-ops:x   /home/ubuntu /home/ubuntu/blossoML
sudo setfacl -m u:blossoml-ops:rx  /home/ubuntu/blossoML/data /home/ubuntu/blossoML/db
sudo setfacl -m u:blossoml-ops:r   /home/ubuntu/blossoML/db/blossom.db
sudo setfacl -m u:blossoml-ops:r   /home/ubuntu/blossoML/main.exe /home/ubuntu/blossoML/main.exe.backup
```

注意:
- `main.exe` はデプロイのたびに `mv` で置き換わるため ACL が消えます。`main.exe` に `o+r` を付けるか（バイナリは秘密ではない）、
  blossoML の deploy.yml の差し替え後に `setfacl -m u:blossoml-ops:r main.exe main.exe.backup` を追加してください。
  付いていない場合、`deploy_info` はハッシュを返せません（エラーになるだけで他は動きます）。
- SQLite を読み取り専用で開けるか確認します（WAL モードだと `-shm` の作成が必要になり失敗することがあります）。

  ```sh
  sudo -u blossoml-ops python3 -c "import sqlite3; c=sqlite3.connect('file:/home/ubuntu/blossoML/db/blossom.db?mode=ro', uri=True); print(c.execute('select count(*) from blobs').fetchone())"
  ```

  失敗する場合は `db/` に `-shm` / `-wal` 用の ACL（`rw`）が必要か、`journal_mode` を確認してください。

### I1 の確認（受け入れ条件）

```sh
F=$(sudo ls /home/ubuntu/blossoML/data | grep -E '^[0-9a-f]{64}$' | head -1)
sudo -u blossoml-ops python3 -c "open('/home/ubuntu/blossoML/data/$F','rb')"   # → PermissionError になること
sudo -u blossoml-ops ls /home/ubuntu/blossoML/data | head -3                       # 一覧はできること
```

## 3. 外向き通信（7.3, I4）

```sh
sudo install -m 0644 proxy/tinyproxy.conf /etc/tinyproxy/tinyproxy.conf
sudo install -m 0644 proxy/blossoml-ops.filter /etc/tinyproxy/blossoml-ops.filter
sudo systemctl restart tinyproxy

sudo install -m 0644 nftables/blossoml-ops.nft /etc/nftables.d/blossoml-ops.nft   # /etc/nftables.conf から include する
sudo nft -f /etc/nftables.d/blossoml-ops.nft
sudo systemctl enable --now nftables
```

blossoML が 8082 以外で待ち受けている場合は `.nft` のポートと `config.yaml` の `blossoml.url` を合わせてください。

### I4 の確認（受け入れ条件）

```sh
sudo -u blossoml-ops curl -sS -m 5 https://example.com/ ; echo "exit=$?"                       # 失敗すること（直接）
sudo -u blossoml-ops curl -sS -m 5 -x http://127.0.0.1:8888 https://example.com/ ; echo "exit=$?"  # 失敗すること（403）
sudo -u blossoml-ops curl -sS -m 5 -x http://127.0.0.1:8888 https://api.anthropic.com/ -o /dev/null -w '%{http_code}\n'  # 応答があること
sudo -u blossoml-ops curl -sS -m 5 http://localhost:8082/ | head -2                            # blossoML には直接届くこと
```

## 4. 設定と systemd（7.4）

```sh
sudo install -d -o root -g blossoml-ops -m 0750 /etc/blossoml-ops
sudo install -m 0644 config.example.yaml /etc/blossoml-ops/config.yaml   # test_blob_sha256 などを記入
sudo install -m 0644 systemd/blossoml-ops@.service /etc/systemd/system/
sudo install -m 0644 systemd/blossoml-ops-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable blossoml-ops-daily.timer   # 起動は 6 章の秘密情報の後（sudo systemctl start blossoml-ops-daily.timer）
```

スワップが未設定なら 1GB 用意します。

```sh
sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 5. アプリケーションの配置（7.7）

```sh
sudo install -d -o ubuntu -g ubuntu -m 0755 /opt/blossoml-ops /opt/blossoml-ops/releases /opt/blossoml-ops/incoming /opt/blossoml-ops/bin
sudo install -o root -g root -m 0755 bin/run-manual /opt/blossoml-ops/bin/run-manual
```

以降は `v*` タグの push で CI が配備します（`remote-install.sh`）。配備物は conda-pack で固めた実行環境（Python、conda-forge の Spot、依存パッケージ、本体）で、本番では展開して `conda-unpack` するだけです（pip もネットワークも使いません）。ここで最初のタグを push して初回の配備を済ませます。配備ユーザー（ubuntu）は `sudo -u blossoml-ops` で selfcheck を実行できる必要があります。
`blossoml-ops` ユーザーは `/opt/blossoml-ops` を読めるだけで、自分のコード・ルール・DFA を書き換えられません。

## 6. 秘密情報（7.5）

```sh
# Anthropic API キー
sudo install -o blossoml-ops -g blossoml-ops -m 0400 /dev/stdin /etc/blossoml-ops/anthropic_api_key <<< 'sk-ant-...'
# bot 専用の Nostr 鍵（新規生成。運営者や他用途の鍵は使い回さない）
sudo -u blossoml-ops /opt/blossoml-ops/current/env/bin/python -m blossoml_ops.main keygen --out /var/lib/blossoml-ops/nsec.tmp
sudo install -o blossoml-ops -g blossoml-ops -m 0400 /var/lib/blossoml-ops/nsec.tmp /etc/blossoml-ops/nostr_bot_nsec
sudo rm /var/lib/blossoml-ops/nsec.tmp
```

`keygen` が表示する npub を控えておきます。続けて、秘密情報を含めた自己診断と、bot のプロフィール（kind 0）の公開を行います。

```sh
sudo -u blossoml-ops env BLOSSOML_OPS_CONFIG=/etc/blossoml-ops/config.yaml \
  /opt/blossoml-ops/current/env/bin/python -m blossoml_ops.main selfcheck
sudo -u blossoml-ops env BLOSSOML_OPS_CONFIG=/etc/blossoml-ops/config.yaml \
  /opt/blossoml-ops/current/env/bin/python -m blossoml_ops.main publish-profile
```

運営者側: 運営者の kind 10050（DM 受信リレー）に、`relay.primal.net` / `relay.damus.io` / `nos.lol` / `nostr.mom` の少なくとも 1 つが含まれていることを確認してください。

## 7. 実行

```sh
sudo systemctl start blossoml-ops@daily          # 定期点検を今すぐ
sudo systemctl start blossoml-ops@postdeploy     # デプロイ後の確認
sudo /opt/blossoml-ops/bin/run-manual "直近 6 時間の 5xx の内訳を調べて"
journalctl -u 'blossoml-ops@*' -n 50             # 実行結果（終了コードと費用）
ls /var/lib/blossoml-ops/traces/                 # トレース（JSONL）
```

終了コード: 0 正常 / 1 内部エラー / 2 ルール未達（義務が残った）/ 3 報告が 1 通も届かなかった（`undelivered.jsonl` に記録）/ 4 ハーネスが異常を検知（出口検査違反、R0/R6 の発火など。critical DM 済み）/ 5 設定・ルールの不整合。

毎日の DM が届かないことが、サーバ停止を含む異常の唯一の目安です（外部からの死活監視は作らない方針）。

## 8. メモリ実測（受け入れ条件）

```sh
sudo systemctl start blossoml-ops@daily &
for i in $(seq 1 60); do systemctl show blossoml-ops@daily -p MemoryPeak -p MemoryCurrent; systemctl show blossoml -p MemoryCurrent; sleep 5; done
```

`MemoryPeak` と blossoML の `MemoryCurrent` を記録し、同居を続けるか、エージェントを別インスタンスに移すかの判断材料にします。

## 9. ロールバック

エージェント自体（blossoml-ops）の版を戻す:

```sh
cd /opt/blossoml-ops && ls -1t releases/          # 直近 3 版が残っている
ln -sfn releases/<前の版> current.tmp && mv -T current.tmp current
```

oneshot なので再起動は不要です。次の起動から前の版が使われます。

（blossoML 本体の `main.exe.backup` へのロールバックは別物です。人間の承認フローが今回のスコープ外のため、エージェントにはロールバック用のツールも sudoers の許可もありません。エージェントは報告の中で提案するだけです。）

## 10. フェーズ 2（書き込み系ツール）を有効にする場合

```sh
sudo install -d /usr/local/libexec/blossoml-ops
sudo install -o root -g root -m 0755 libexec/backup-db libexec/cleanup-tmp /usr/local/libexec/blossoml-ops/
sudo install -m 0644 systemd/blossoml-ops-backup-db.service 'systemd/blossoml-ops-cleanup-tmp@.service' /etc/systemd/system/
sudo systemctl daemon-reload
sudo visudo -cf sudoers/blossoml-ops && sudo install -o root -g root -m 0440 sudoers/blossoml-ops /etc/sudoers.d/blossoml-ops
```

バックアップと tmp 掃除は、エージェントが sudo で「root の systemd ユニットを起動する」形で行います。処理はエージェントのサンドボックス（`ProtectHome=read-only` など）の外で動きます。sudo で直接スクリプトを動かすと、エージェントの制限がスクリプトにも引き継がれて書き込めません（実験環境で確認）。

- `/etc/blossoml-ops/config.yaml` で `phase2.enabled: true`
- `blossoml-ops@.service` の `NoNewPrivileges=yes` を外す（sudo と両立しないため）。`ReadWritePaths` はそのままで構いません（書き込みは root のスクリプトが行う）。
- 有効化すると R1（再起動前の確認）、R2（再起動は 1 回）、R4（再起動後に稼働を確認する。結果が 200 でなくてもよい）、R5（掃除の前のバックアップ）が意味を持ちます。
