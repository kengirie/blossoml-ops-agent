# blossoML 運用保守エージェント ＋ LTL ガードライブラリ 設計書

この文書は、Claude Code に実装を依頼するための設計書です。
実装者は、まず「2. 絶対に守る不変条件」を読み、どの変更もこれを破らないことを確認してから作業してください。

- 対象サーバ: blossoML（https://github.com/kengirie/blossoML 、OCaml 製の Blossom サーバ）
- 実装言語: Python 3.10 以上
- エージェント基盤: Claude Agent SDK（Python 版 `claude-agent-sdk`）
- 設計時点のリポジトリ: blossoML コミット `64b236c`

---

## 1. 目的と範囲

### 1.1 作るもの

1. **LTL ガードライブラリ（仮称 `ltlguard`）**
   エージェントのツール呼び出しに対するルールを LTLf（有限長の実行の上で解釈する線形時相論理）で書き、
   - 実行時: Agent SDK の hook で**強制**する
   - 実行後: 保存したトレースを再生して**評価**する
   の両方を、同じルールファイルで行えるライブラリ。将来 OSS として公開する前提で、コア部分は公開品質で作る。

2. **blossoML 運用保守エージェント（仮称 `blossoml-ops`）**
   blossoML の本番サーバ上で 1 日 1 回動き、点検結果を運営者に Nostr の暗号化 DM（NIP-17）で報告する。
   `ltlguard` の最初の利用者（dogfooding）でもある。

3. **blossoML 本体への前提変更（小さな PR）**
   エージェント導入の前提となるパーミッション修正など（「8. blossoML 本体への変更」）。

### 1.2 エージェントの仕事

| 優先度 | 仕事 | フェーズ |
|---|---|---|
| 1 | 定期点検（1 日 1 回）: 稼働状況、`GET /` の応答、ディスク、エラーログの傾向、`tmp_*` の残骸、DB とファイルの食い違い | 1 |
| 2 | デプロイ後の確認（スモークテスト。運営者が用意した無害なテスト用 blob のみ使用） | 1（手動起動） |
| 3 | 障害の一次対応（再起動、ロールバックの提案と承認後の実行） | 2 |

### 1.3 やらないこと（明示的に範囲外）

- **通報（BUD-09 / `blob_reports`）への対応は一切しない。** `blob_reports` テーブルにも触れない。
- blob の削除、隔離はしない。
- 外部からの死活監視（GitHub Actions 等）は作らない。サーバごと停止した場合に検知できないリスクは受け入れ済み。毎日の DM が届かないことが唯一の目安。

---

## 2. 絶対に守る不変条件（MUST）

以下はすべての実装・設定・テストで守ること。どれかを緩める変更は、運営者の明示的な承認なしに行ってはならない。

### I1. blob の中身は、決して Claude（Anthropic API）に届かない

ツールの実行結果はすべて API に送られる。blob には違法・危険な画像が含まれうるため、**blob のバイト列がツールの出力に入る経路が存在しない**ことを、以下の多層で保証する。1 層が破れても他の層で止まること。

1. **ツール設計**: blob ファイルを開くツールを作らない。出力は件数、バイト数、ファイル名（sha256）、時刻、HTTP ステータス等に限る。
2. **OS パーミッション**: blob ファイルは `0640`、エージェントの OS ユーザー `blossoml-ops` は blob の所有グループに属さない。ディレクトリには ACL で「通過・一覧」だけを許可し、ファイル本体の読み取り権限は与えない。
3. **出口検査（output guard）**: すべてのツールは、結果を返す直前に共通の検査を通す（「6.3 出口検査」）。検査は hook ではなく**ツール関数の内側**で行う。
4. **外界への道具を渡さない**: 組み込みツール（Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, NotebookEdit, Task/Agent 等すべて）を無効化する。`http_probe` はパスを許可リストで固定し、テスト用 blob 以外の sha256 にはアクセスしない（HEAD も不可）。
5. **LTL による監視**: R0（ツール許可リスト）と R6（probe の許可リスト）を PreToolUse hook で強制する。第 1〜4 層が正しければ発火しないはずなので、**発火したらバグとして critical 通知する**。

補足: この性質は情報フロー（非干渉性）の性質であり LTL では表現できない。LTL はあくまで呼び出しレベルの第 2 防壁。保証の本体は 1〜4 の構造。

### I2. Claude は自作ツールしか使えない。シェルはどこにも出てこない

- SDK 設定で組み込みツールを無効化し、権限モードは `dontAsk` 相当（許可リスト外は確認せず拒否）にする。
  注意: `allowed_tools` は「確認なしで実行してよいもの」を決めるだけで、**書かなかったツールが消えるわけではない**。組み込みツールは禁止リスト（または組み込みツール一式を外すオプション。要確認）で明示的に外すこと。
- PreToolUse hook で、自作ツール名の許可リストにない呼び出しはすべて拒否する（R0）。
- **ツールの実装でもシェルを使わない。** `subprocess` は `shell=False` で、引数は固定のリストで渡す。可能なものは Python ライブラリで直接行う。
- Claude から受け取る引数は、列挙型・範囲つき数値・長さ上限つき文字列に限定し、厳格に検証する。自由なパス、自由なコマンド、自由な SQL は受け取らない。
- `blossoml-ops` のログインシェルは `/usr/sbin/nologin`。

### I3. 秘密情報は Claude に渡さない

Anthropic API キー、bot の Nostr 秘密鍵は、ツールの内部とハーネスだけが読む。プロンプト、ツールの出力、トレースに含めない。

### I4. 外向き通信は許可リストのみ

`blossoml-ops` ユーザーの通信は、iptables の owner マッチでローカルの転送プロキシ宛て以外を遮断し、プロキシはドメインの許可リストで以下だけを通す。

- `api.anthropic.com`
- `relay.primal.net`, `relay.damus.io`, `nos.lol`, `nostr.mom`

`http_probe` は外へ出ず、`localhost` の blossoML に直接接続する。

### I5. blossoML を道連れにしない

本番は 1 OCPU / メモリ 1GB。メモリが尽きたときは**必ずエージェントが先に死ぬ**よう、systemd で `MemoryMax`、`OOMScoreAdjust`、`CPUQuota`、`Nice` を設定する（「7.4 systemd」）。エージェントは常駐させず、timer から起動する oneshot とする。

### I6. 失敗は安全側に倒す（fail-closed）

LTL モニタ、出口検査、引数検証のいずれかで例外・タイムアウトが起きた場合は、**拒否**として扱う。

---

## 3. 本番環境の前提（blossoML のコードから確認済み）

| 項目 | 内容 | 根拠 |
|---|---|---|
| サーバ | Oracle Cloud、1 OCPU / 1GB。CPU アーキテクチャは要確認（`uname -m`） | 運営者より |
| バイナリ | `/home/ubuntu/blossoML/main.exe`、直前版は `main.exe.backup` | `.github/workflows/deploy.yml` |
| プロセス管理 | systemd ユニット `blossoml` | 同上 |
| デプロイ | `v*` タグの push で GitHub Actions がビルド→scp→差し替え→再起動 | 同上 |
| データ | 作業ディレクトリ直下の `data/`（blob 本体、ファイル名は sha256。アップロード中は `tmp_<n>_<n>`）と `db/blossom.db`（SQLite） | `bin/main.ml`, `lib/shell/storage_eio.ml` |
| DB スキーマ | `blobs(sha256, uploaded_at, status ∈ {stored, deleted, quarantined}, remote_url, mime_type, size)`、`blob_owners`、`blob_reports`（**触らない**） | `lib/shell/blossom_db.ml` |
| 削除 | 論理削除（`status='deleted'`）、所有者 0 なら物理ファイルも unlink | `lib/shell/blob_service.ml` |
| ログ | `Eio.traceln` で stderr へ。systemd 経由で journald に入る。主なパターン: `Request:`, `Response: ... -> <status>`, `Upload successful`, `Delete request`, `Save failed`, `Mirror failed`, `SHA256 mismatch`, `MIME type policy violation`, `WARNING: Failed to unlink`, `Report received` | `lib/shell/http_server.ml` 他 |
| ヘルスチェック | 専用エンドポイントなし。`GET /` が利用規約テキストを返す | `lib/shell/http_server.ml` |
| ポリシー | 上限 100MB、MIME 制限なし、公開鍵の許可リストなし（誰でもアップロード可） | `lib/core/policy.ml` |
| ファイル権限 | blob と一時ファイルを `0o644` で作成している（**要修正**） | `lib/shell/storage_eio.ml` |
| バックアップ | DB のバックアップ手順はない | `deploy.yml` |

---

## 4. 全体構成

```
本番サーバ（Oracle Cloud, 1 OCPU / 1GB）
├ blossoML（systemd: blossoml）                       ユーザー: ubuntu
│   ├ /home/ubuntu/blossoML/data/   blob 0640      ← blossoml-ops は中身を読めない
│   └ /home/ubuntu/blossoML/db/blossom.db
│
└ blossoml-ops（systemd timer → oneshot）              ユーザー: blossoml-ops（新設, nologin）
    ├ Claude Agent SDK（組み込みツールすべて無効、setting_sources=[]）
    ├ 自作ツール（in-process、シェル不使用、出口検査つき）
    ├ ltlguard モニタ（PreToolUse / PostToolUse / 終了時）
    ├ notify → Nostr リレー 4 つ（NIP-17 DM）
    ├ トレース出力（JSONL）
    └ 外向き通信: iptables owner match → ローカル転送プロキシ → 許可ドメインのみ
```

### 4.1 リポジトリ構成（提案）

コアを SDK に依存させないこと、本番固有の情報をライブラリに混ぜないことを最優先にする。最初は 1 リポジトリ内で分け、2 つ目の用途が出た時点で `ltlguard` を別リポジトリに切り出す。

```
ltlguard/                  # 将来 OSS。本番固有情報は一切置かない
  ltlguard/core/           # LTLf パース、DFA 変換、状態遷移、4 値判定。SDK を import しない
  ltlguard/spec/           # ルールファイル（YAML）の読み込みとテンプレート展開
  ltlguard/trace/          # トレース JSONL の書き込み・読み込み
  ltlguard/adapters/claude_agent_sdk/   # hook との接続。SDK 依存はここだけ
  ltlguard/eval/           # 評価モード（フェーズ 3）
  tests/
blossoml_ops/              # blossoML 専用。ホスト名・パス等はここと設定ファイルだけ
  blossoml_ops/tools/      # 自作ツール
  blossoml_ops/guard.py    # 出口検査
  blossoml_ops/notify/     # NIP-17 送信
  blossoml_ops/labeling.py # ツール呼び出し → 命題 の対応（ラベル付け関数）
  blossoml_ops/prompt.md   # システムプロンプト
  blossoml_ops/rules.yaml  # LTL ルール
  blossoml_ops/main.py     # エントリポイント（モード: daily / postdeploy / manual）
  deploy/                  # systemd ユニット、sudoers、プロキシ設定、root 所有スクリプト、セットアップ手順、remote-install.sh
  tests/
.github/workflows/deploy.yml   # タグ push で テスト → Spot でルールをコンパイル → パッケージ → 配備（7.7）
```

ツールの実行場所は抽象化し、「ローカル実行」と「SSH 経由実行」を差し替えられるようにする（メモリ不足で別 VM へ移す場合に備える）。フェーズ 1 ではローカル実行のみ実装すればよい。

---

## 5. ltlguard（LTL ガードライブラリ）

### 5.1 ルールファイル

形式は YAML（初期案。変更可）。命題の**定義**は Python 側（ラベル付け関数）で行い、YAML は命題の**名前**だけを参照する。

```yaml
# 構文のイメージ（確定ではない）
version: 1
rules:
  - id: R1
    name: 再起動の前に状態とログを確認する
    description: >
      restart_service を呼ぶ前に、service_status と read_logs を実行してください。
    formula: "!restart W (status_checked & logs_read)"
    on_violation: deny            # deny | ask_human | log_only
  - id: R4
    name: 再起動・ロールバック後は稼働確認する
    template: response            # よく使う形はテンプレートで書ける
    trigger: "restart | rollback"
    response: probe_ok
```

- `description` は、拒否時に Claude へ返すメッセージの本文になる。
- テンプレート: `never(a)`、`not_until(a, b)`（= `¬a W b`）、`response(a, b)`（= `G(a → F b)`）、`at_most_once(a)`（= `G(a → X G ¬a)`）、`eventually(a)`。生の LTLf も書ける。
- `on_violation: log_only` は「影モード」（強制せず記録のみ）。

### 5.2 命題（ラベル付け関数）

ツール呼び出しのイベントを、真である命題の集合に写す関数。利用者（blossoml_ops）が登録する。

- **呼び出し時に決まる命題**: ツール名と引数だけで決まる（例: `restart` = `restart_service` が呼ばれた）。PreToolUse で評価できる。
- **結果で決まる命題**: ツールの結果を見て決まる（例: `probe_ok` = `http_probe` が 200 を返した、`notified` = 1 つ以上のリレーが OK を返した）。PostToolUse で評価する。
- ハーネス由来の命題: `approved`（人間の承認。フェーズ 2）など、ツール以外の出来事もイベントとして入れられるようにする。

### 5.3 コンパイル（Spot を使用）

LTLf → DFA（決定性有限オートマトン）の変換には **Spot**（LRDE が開発する、LTL と ω オートマトンを扱う C++ ライブラリ。Python バインディングあり）を使う。

**コンパイル時と実行時を分ける**

```
[開発機 / CI]                               [本番]
rules.yaml ──Spot──▶ DFA ──前計算──▶ rules.dfa.json ──▶ 純 Python のモニタ
（ltlguard compile）                          （Spot に依存しない）
```

- Spot を使うのは `ltlguard compile` コマンド（開発機・CI）だけ。**本番のランタイムは Spot を import しない。** 理由:
  - 本番は 1GB で、Spot のビルド・インストールは重い
  - Spot は pip で入れられず、ディストリビューションのパッケージ、conda、ソースビルドのいずれかになる（インストール方法は実装時に確認）
  - ライセンス（下記）の影響範囲をコンパイラに閉じ込められる
- `rules.dfa.json` は生成物としてリポジトリにコミットし、`rules.yaml` との対応はハッシュで検証する（`rules.yaml` を変えて再コンパイルし忘れたら、実行時に起動を拒否する）。

**変換の経路（優先順）**

1. **MTDFA 経路（第一候補）**: Spot 2.14（2025-06 リリース）で追加された LTLf → MTDFA（多端末 BDD で表現した DFA）変換。C++ では `spot::ltlf_to_mtdfa()`、最小化は `spot::minimize_mtdfa()`。高速で、DFA の最小化も含まれる。**Python バインディングでの正確な関数名と、MTDFA から明示的な状態・遷移を取り出す方法は要確認**。コマンドラインの `ltlf2dfa` ツールも同じ機能を持つ。
2. **LTL 経由の経路（代替）**: `spot.from_ltlf(f)` で LTLf を「alive」命題つきの LTL に変換 → `translate` で決定性オートマトンを作る → `spot.to_finite()` で有限語のオートマトンに戻す。Spot の公式チュートリアル（tut12）に Python の例がある。MTDFA の Python API が使いにくい場合はこちらを使う。

Spot のバージョンは 2.14 以上を要求する（経路 1 を使う場合）。

**JSON に書き出す内容**

- 原子命題の一覧
- 状態の一覧と初期状態
- 遷移: `(現在の状態, ガード, 次の状態)`。ガードは命題上の論理式（Spot の BDD を論理式の文字列にしたもの）。実行時は、その時点で真の命題の集合を代入して評価する。ガードの評価器は小さな自前の実装でよい（`&`, `|`, `!`, 命題, `true`, `false` のみ）
- 各状態について前計算した値:
  - `accepting`: ここで終われば成立
  - `dead`: ここからどう進んでも受理状態に到達できない（到達可能性で計算）
  - `safe_next`: 次に dead に落ちない遷移のガード
- 元の LTLf 式、ルール ID、`rules.yaml` のハッシュ、Spot のバージョン

注意: Spot の有限語オートマトンは、受理を「状態」ではなく「最後の遷移」に付ける表現をとる場合がある（`ltlf2dfa` のドキュメントでは、遷移ベースの表現で「最後の遷移が受理なら受理」）。JSON に書き出すときに、ltlguard の意味論（状態ベースの `accepting`）へ正しく変換すること。変換の正しさは、ランダムに生成した短いトレースについて、Spot 側の判定と ltlguard のモニタの判定が一致することをテストで確認する（差分テスト）。

**ライセンス**

Spot は GPL v3 以上で配布されている。ltlguard を OSS で公開する際、Spot を import するのはコンパイラだけに限定し、ランタイムは Spot に依存させない。コンパイラ部分を含めた ltlguard 全体のライセンスをどうするか（GPL 互換にする、コンパイラを別パッケージにする等）は、公開前に運営者が判断する。

### 5.4 実行時モニタの意味論

- **PreToolUse**: 呼び出し時の命題で 1 歩を**試算**する（状態は確定しない）。試算先が `dead` なら `on_violation` に従う（`deny` なら拒否）。
- **PostToolUse**: 呼び出し時＋結果の命題で 1 歩を**確定**する。
- 状態を Pre で進めない理由: 拒否された呼び出しや、他の仕組みで止められた呼び出しで状態が進むのを防ぐため。
- 判定は 4 値: 確定成立 / 確定違反 / 暫定成立（今終われば成立）/ 暫定違反（今終われば違反＝未履行の義務あり）。
- **終了時**: 暫定違反のルールがあれば終了を差し戻す（未履行の義務を Claude に伝える）。差し戻しは上限回数（例: 2 回）まで。超えたら「ルール未達で失敗」として終了し、critical 通知する。SDK で終了を差し戻す正確な方法（Stop 系の hook の挙動）は要確認。確認できない場合は、ハーネス側のループで「結果メッセージ受信後に義務が残っていれば追加の指示を送る」形で実装する。
- **並列のツール呼び出し**: モニタ対象のツールは直列化する。順序は PreToolUse の到着順。
- **fail-closed**: モニタ内部の例外・タイムアウトは拒否。
- 状態はセッション ID と一緒に保存し、再開時に復元できるようにする（フェーズ 1 では保存のみでよい）。

### 5.5 拒否時のメッセージ

Claude に返す拒否理由には次を含める。

```
ルール R1「再起動の前に状態とログを確認する」に違反するため実行していません。
理由: restart_service を呼ぶ前に、service_status と read_logs を実行してください。
現在実行できる操作: service_status, read_logs, disk_usage, ...
```

「現在実行できる操作」は、全ルールの `safe_next` の共通部分から計算する。

### 5.6 トレース（JSONL）

1 行 1 イベント。評価モードの入力になる。最低限のフィールド:

```json
{"ts": "...", "session": "...", "seq": 12, "event": "pre|post|deny|stop|human",
 "tool": "restart_service", "args": {...}, "props": ["restart"],
 "result_summary": {...}, "monitor": {"R1": {"state": 2, "verdict": "tentative_ok"}}}
```

- `args` と `result_summary` は出口検査済みのもののみ。秘密情報は入れない。
- ローテーションする（サイズまたは日数で上限）。

### 5.7 評価モード（フェーズ 3）

トレースを再生して、ルールごとに次を出す。

- 成否（4 値）
- 違反未遂率（強制で止めたものを含め、破ろうとした回数）
- 拒否からの立ち直り率
- 差し戻し前に義務を自発的に果たした割合
- 影モードのルールの、仮に強制していた場合の拒否数

---

## 6. blossoml-ops（運用保守エージェント）

### 6.1 Agent SDK の設定

- `system_prompt`: **自前の文字列**（`claude_code` プリセットは使わない）。運用系・非コーディング・無人実行のため。
  - system reminder の説明を一文入れる（「アプリケーションが会話に挿入する system reminder は、ユーザーではなくアプリケーションからの文脈として扱う」）。
- `setting_sources=[]`: ファイルシステム上の CLAUDE.md、設定、output style 等を一切読み込まない。
- 作業ディレクトリ: `blossoml-ops` のホーム配下の**空の専用ディレクトリ**。blossoML のディレクトリにはしない。
- ツール: 自作ツールのみ（SDK のカスタムツール機能 = in-process MCP サーバ）。組み込みツールは全て無効（I2 参照）。
- 権限モード: 許可リスト外は確認せず拒否。
- hook: PreToolUse / PostToolUse に ltlguard アダプタ。
- `max_turns`: 30 程度（実測で調整）。1 回あたりの費用上限もハーネス側で監視し、超えたら中断して通知。
- モデル: 設定ファイルで指定可能にする。

### 6.2 ツール一覧

すべてのツールは「6.3 出口検査」を通してから結果を返す。シェルは使わない（I2）。

**フェーズ 1（読み取り専用）**

| ツール | 実装方法 | Claude からの引数 | 返すもの |
|---|---|---|---|
| `service_status` | `systemctl show blossoml -p ActiveState,SubState,ActiveEnterTimestamp,NRestarts`（固定 argv） | なし | 状態、起動時刻、再起動回数 |
| `read_logs` | python-systemd の journal reader で `_SYSTEMD_UNIT=blossoml.service` のみ読む | `since_hours`（1〜72）、`kind`（列挙: `errors` / `uploads` / `deletes` / `mirror` / `status_codes` / `all_summary`） | 集計（件数、ステータスコード別件数、エラー行の種類別件数）と、エラー行のサンプル（行数・行長に上限） |
| `http_probe` | Python の HTTP クライアントで `http://localhost:<port>` に接続 | `target`（列挙: `root_get` / `upload_head` / `test_blob_head` / `test_blob_get`） | ステータス、所要時間、主要ヘッダ、`root_get` のみ本文の先頭 N 文字。`test_blob_get` は本文のハッシュ一致の真偽のみ |
| `disk_usage` | `os.statvfs` と、`data/` `db/` 配下のサイズ合計（`os.scandir` + `os.stat`、中身は開かない） | なし | 空き容量、使用量 |
| `storage_audit` | `os.scandir` / `os.stat` | なし | ファイル数、`tmp_*` の一覧（名前・経過時間・サイズ）、命名規則に合わないファイル数 |
| `db_report` | `sqlite3` を URI `file:...?mode=ro` で開き、名前付きの固定クエリのみ実行 | `name`（列挙: `blob_counts_by_status` / `recent_uploads_count` / `owner_counts` / `consistency`） | 集計値。`consistency` は「DB で stored だがファイルなし」「ファイルはあるが DB になし」の件数と sha256 の一覧（上限あり） |
| `deploy_info` | `os.stat` と SHA-256 計算（`main.exe` と `main.exe.backup`。blob ではない） | なし | 更新時刻、ハッシュ |
| `notify` | 「6.4 Nostr 通知」 | `severity`（`info` / `warning` / `critical`）、`title`（≤100 文字）、`body`（≤2000 文字） | 受理したリレー数、失敗したリレー |

- `blob_reports` テーブルを参照するクエリは作らない。
- `test_blob_*` の sha256 は設定ファイルで 1 つだけ指定する（運営者が用意した無害な短いテキスト）。それ以外の sha256 へのアクセス手段は存在しない。
- `read_logs` のサンプル行は、出口検査に加えて長さを切り詰める。公開鍵（64 桁 hex）はマスクするかどうかを設定で切り替え可能にする（既定: マスクしない。要確認）。

**フェーズ 2（書き込み系。LTL 強制と同時に導入）**

| ツール | 実装方法 | 引数 |
|---|---|---|
| `backup_db` | `sudo /usr/local/libexec/blossoml-ops/backup-db`（root 所有。SQLite のオンラインバックアップで日時付きファイルを本番側に作る） | なし |
| `restart_service` | `sudo /usr/bin/systemctl restart blossoml` | なし |
| `rollback_binary` | `sudo /usr/local/libexec/blossoml-ops/rollback`（`main.exe.backup` に戻して再起動） | なし |
| `cleanup_tmp` | `sudo /usr/local/libexec/blossoml-ops/cleanup-tmp <hours>`（スクリプト側でも範囲検査） | `min_age_hours`（6〜168） |
| `request_approval` | 承認依頼の DM を送り、運営者の返信を待つ（「6.5 承認」） | `action`（列挙）、`reason`（≤500 文字） |

- sudoers には argv まで完全に固定して書く。`cleanup_tmp` の数値引数のみ例外とし、スクリプト側で検証する。
- root 所有スクリプトは `blossoml-ops` から書き換えられない場所・権限で置く。
- blob の削除ツールは作らない。

### 6.3 出口検査（output guard）

全ツール共通。ツール関数の内側、結果を返す直前に実行する。

1. 出力を JSON シリアライズ可能な構造（dict / list / str / int / float / bool）に限定する。`bytes` は禁止。
2. 全体のサイズ上限（例: 16KB）。超えたら切り詰めではなく、集計のみに縮退するか拒否。
3. すべての文字列について:
   - UTF-8 として正しいこと
   - 制御文字（改行・タブ以外）を含まないこと
   - 画像等のマジックナンバーを含まないこと（PNG `\x89PNG`、JPEG `\xFF\xD8\xFF`、GIF `GIF8`、WebP `RIFF....WEBP`、MP4 `ftyp`、PDF `%PDF` 等。文字列化された表現も含めて検出）
   - 長い base64 / hex 風の連続（例: 200 文字以上で該当文字種のみ）を含まないこと。ただし 64 桁 hex（sha256・公開鍵）は許可
4. 違反した場合: 結果を破棄し、Claude には「出力を破棄した（理由の種類のみ）」とだけ返す。同時に critical 通知とトレース記録（中身は記録しない）。

テスト必須: 各種画像ヘッダ、base64 化した画像、バイナリを含む入力が確実に拒否されること。

### 6.4 Nostr 通知（NIP-17）

- 方式: NIP-17 のプライベート DM。本文（kind 14 の rumor）を NIP-44 v2 で暗号化して kind 13 の seal に入れ、さらに NIP-59 の gift wrap（kind 1059、使い捨て鍵で署名）で包む。NIP-04（kind 4）は使わない。
- 送信元: **bot 専用の新しい鍵ペア**。運営者や他用途の鍵は使い回さない。プロフィール（kind 0）に「blossoML ops bot」等を設定しておく（セットアップ手順に含める）。
- 送信先（固定。Claude には選ばせない）:
  - npub: `npub1svne455wa3rctcsnnhzjn2t9pldmgfpkd4rytewzsf8he02fysxsvgwhj4`
  - hex: `83279ad28eec4785e2139dc529a9650fdbb424366d4645e5c2824f7cbd49240d`
- リレー（固定）: `wss://relay.primal.net`, `wss://relay.damus.io`, `wss://nos.lol`, `wss://nostr.mom`
- 成功判定: NIP-01 の `OK` メッセージで、**1 つ以上のリレーが受理**したら成功。これが命題 `notified`（結果で決まる命題）になる。
- 失敗時: 再送（指数バックオフ、合計の上限時間あり）。全滅なら、ローカルのファイルに記録し、プロセスを非ゼロ終了コードで終える。
- NIP-42（リレー認証）やレート制限を要求するリレーがありうるので、実装時に各リレーの挙動を確認する。
- 本文の整形はハーネスが行う: `[<severity>] blossoML: <title>\n\n<body>\n\n(run: <session id>, <時刻>)`
- 送信回数の上限: `info` はセッションあたり 1 通、`warning` / `critical` は合計 5 通まで（LTL ルールでも表現する）。
- 暗号まわりは、NIP-44 / NIP-59 を実装した保守されている Python ライブラリがあればそれを使う。なければ仕様に従って実装し、**NIP-44 の公式テストベクタで必ず検証する**。
- 運営者側の前提: 運営者の kind 10050（DM 受信リレーの一覧）に、上の 4 つのうち少なくとも 1 つが含まれていること（含まれないと、クライアントに表示されないことがある）。

### 6.5 人間の承認（フェーズ 2）

1. `request_approval` が、使い捨ての確認コード（例: 6 桁）を含む承認依頼 DM を送る。
2. ハーネスがリレーを購読し、bot 宛ての NIP-17 DM を受け取る。
3. **ハーネスが**、送信者の公開鍵が運営者のものであること、本文に確認コードが含まれること、有効期限内であることを検証する。満たした場合のみ、イベント `approved(action)` をモニタに入れる。
4. Claude には「承認された／されなかった／期限切れ」という結果だけを返す。**返信本文は Claude に渡さない**（任意の人が bot に DM を送れるため、プロンプトインジェクションの入口になる）。

### 6.6 システムプロンプトの骨子

`blossoml_ops/prompt.md` に置く。含める内容:

- 役割: blossoML 本番サーバの運用点検担当。出力は運営者への報告。
- サーバの構成（「3. 本番環境の前提」の要約）と、正常時のログのパターン。
- 実行モードごとの手順（daily / postdeploy / manual）。
- 重大度の判断基準（例: サービス停止・`GET /` 失敗 → critical、ディスク空き 10% 未満・エラー急増 → warning、それ以外 → info）。
- 報告の書き方（結論を先に、数値は前日比、推測と事実を区別）。
- 制約の説明: blob の中身には触れないこと、ツールの拒否メッセージには従い代替手順を取ること。
- system reminder についての一文。

### 6.7 実行モード

| モード | 起動 | 内容 |
|---|---|---|
| `daily` | systemd timer（1 日 1 回。時刻は設定で指定、要決定） | 定期点検 → `info` 1 通（異常があれば warning / critical） |
| `postdeploy` | 運営者が手動で起動 | テスト用 blob を使ったスモークテスト、デプロイ前後のエラー傾向比較 |
| `manual` | 運営者が手動で起動、追加の指示文を渡せる | 調査（フェーズ 1 は読み取りのみ） |

手動起動は、運営者が本番に SSH して `sudo -u blossoml-ops /opt/blossoml-ops/bin/run <mode>` のように実行する想定。

### 6.8 blossoML 用の LTL ルール

| ID | ルール | 式 | 種類 | フェーズ |
|---|---|---|---|---|
| R0 | 許可リストのツールしか呼ばない | `G allowed_tool` | 安全性 | 1 |
| R6 | probe は許可リストの対象のみ | `G ¬probe_non_allowlisted` | 安全性 | 1 |
| R5' | 終了前に必ず報告する | `F notified` | 活性 | 1 |
| R8 | info 通知は 1 回まで | `at_most_once(notify_info)` | 安全性 | 1 |
| R1 | 再起動の前に状態とログを確認する | `¬restart W (status_checked ∧ logs_read)` | 安全性 | 2 |
| R2 | 再起動は 1 セッション 1 回まで | `G(restart → X G ¬restart)` | 安全性 | 2 |
| R3 | ロールバックは承認後のみ | `¬rollback W approved_rollback` | 安全性 | 2 |
| R4 | 再起動・ロールバック後は稼働確認する | `G((restart ∨ rollback) → F probe_ok)` | 活性 | 2 |
| R5 | tmp 掃除の前に DB をバックアップする | `¬cleanup W backup_ok` | 安全性 | 2 |

R0 と R6 は、I1 / I2 により本来発火しない。発火したら critical 通知する（バグ検出）。

---

## 7. 本番サーバのセットアップ（`deploy/` に手順とファイルを置く）

### 7.1 ユーザーと権限

- `blossoml-ops` ユーザーを作成。ログインシェルは `/usr/sbin/nologin`。`ubuntu` グループには入れない。
- `systemd-journal` グループに追加（ログ読み取りのため。読むのは `read_logs` で blossoml のみに絞る）。
- ACL（`setfacl`）:
  - `/home/ubuntu`、`/home/ubuntu/blossoML`: `blossoml-ops` に `x`（通過のみ）
  - `/home/ubuntu/blossoML/data`: `r-x`（一覧と stat のため）。**ファイルには何も付けない**。デフォルト ACL も付けない
  - `/home/ubuntu/blossoML/db`: `r-x`、`blossom.db`: `r--`
  - `main.exe`、`main.exe.backup`: `r--`（ハッシュ計算のため）
- SQLite を読み取り専用で開けるか（ジャーナルの扱い含む）を実機で検証する。

### 7.2 sudoers

`/etc/sudoers.d/blossoml-ops`（`visudo -c` で検証）に、argv 固定で次のみ許可（フェーズ 2）。

- `/usr/bin/systemctl restart blossoml`
- `/usr/local/libexec/blossoml-ops/backup-db`
- `/usr/local/libexec/blossoml-ops/rollback`
- `/usr/local/libexec/blossoml-ops/cleanup-tmp *`（スクリプト側で引数を厳格に検証）

### 7.3 外向き通信

- 転送プロキシ（tinyproxy 等の軽量なもの）を `127.0.0.1` で待ち受けさせ、ドメインの許可リストを I4 のとおり設定する。
- iptables（または nftables）の owner マッチで、`blossoml-ops` の uid からの外向き通信は、プロキシのポートと `localhost` の blossoML のポート以外をすべて拒否する。DNS もプロキシ側で解決させる。
- エージェントは `HTTPS_PROXY` 等でプロキシを使う。Claude Code CLI と Nostr の WebSocket 接続の両方がプロキシを通ることを確認する。

### 7.4 systemd

- `blossoml-ops-daily.service`（`Type=oneshot`、`User=blossoml-ops`）と `blossoml-ops-daily.timer`。
- リソース制限（値は実測で調整。初期値の目安）:
  - `MemoryMax=350M`、`MemorySwapMax=` は要検討
  - `CPUQuota=50%`、`Nice=10`
  - `OOMScoreAdjust=800`
  - `TimeoutStartSec=` で実行時間の上限
- blossoML 側（ドロップインで追加）: `OOMScoreAdjust=-500`、`UMask=0027`
- スワップファイル 1GB を用意する（未設定なら）。
- ハードニング（可能な範囲で）: `NoNewPrivileges` はフェーズ 2 の sudo と両立しないので、フェーズ 1 のみ有効化を検討。`ProtectSystem=strict`、`PrivateTmp=yes`、`ReadWritePaths=` でトレース等の書き込み先のみ許可。

### 7.5 秘密情報

- `/etc/blossoml-ops/anthropic_api_key`、`/etc/blossoml-ops/nostr_bot_nsec`: 所有者 `blossoml-ops`、`0400`。
- 環境変数経由で渡す場合も、トレースやログに出さない。

### 7.6 ランタイム

- 本番の Python（3.10 以上）をそのまま使う。CI はこれと**同じマイナーバージョン**でビルドする（7.7）。
- Claude Code CLI は `claude-agent-sdk` のプラットフォーム別 wheel に同梱されているものを使う。Node.js が別途必要かどうかは要確認（必要なら本番に入れる手順を 7.1〜7.5 と同じ手動セットアップに含める）。
- Spot は本番に入れない（5.3）。
- CPU アーキテクチャ（x86_64 / aarch64）を確認し、CI のランナーを合わせる。

### 7.7 デプロイ（blossoML と同じく CI でビルドして配備）

blossoML の `deploy.yml`（タグ push → ビルド → scp → 差し替え）と同じ流れにする。**CI が配備するのはアプリケーションだけ**で、root 権限が要るもの（ユーザー、ACL、sudoers、iptables、プロキシ、systemd ユニット）は 7.1〜7.5 の手動セットアップで扱い、CI からは変更できないようにする。

**配置**

```
/opt/blossoml-ops/                  所有者 ubuntu（デプロイ用ユーザー）、blossoml-ops からは読み取りのみ
  releases/<version>/               リリースごとのディレクトリ
    app/                            コード、prompt.md、rules.yaml、rules.dfa.json
    venv/                           本番上で wheelhouse から作った仮想環境
    VERSION, SHA256SUMS
  current -> releases/<version>     実行中の版を指すシンボリックリンク
  incoming/                         アップロードの一時置き場
```

- **`blossoml-ops` ユーザーは、自分のコード・ルール・DFA を書き換えられない**こと（不変条件の補強。ガードを無効化する経路をなくす）。
- systemd ユニットの `ExecStart` は `/opt/blossoml-ops/current/venv/bin/python -m blossoml_ops.main daily` のように `current` を経由する。
- エージェントは oneshot なので、差し替え後の再起動は不要。次の timer 起動から新しい版が使われる。

**ワークフロー（`.github/workflows/deploy.yml`、`v*` タグの push で起動）**

1. **テスト**: ltlguard と blossoml_ops の単体テスト、出口検査のテスト。
2. **ルールのコンパイル**: CI に Spot（2.14 以上、Python バインディングつき）を入れ、`ltlguard compile` で `rules.yaml` → `rules.dfa.json` を生成。**リポジトリにコミットされた `rules.dfa.json` と一致しなければ失敗**させる（ルール変更がレビュー可能な差分として残るように）。差分テスト（5.3）もここで実行。
3. **wheelhouse の作成**: 本番と同じ CPU アーキテクチャ・同じ Python マイナーバージョンで、依存を含むすべての wheel を集める（`pip wheel` 等）。本番がビルドツールなしで `--no-index` インストールできる状態にする。
   - 本番が aarch64 の場合は ARM のランナーを使う。
4. **パッケージ**: `app/`、wheelhouse、`VERSION`、`SHA256SUMS` を tarball にまとめる。
5. **アップロード**: scp で `/opt/blossoml-ops/incoming/` に送る。
6. **本番での展開**（ssh で、リポジトリの `deploy/remote-install.sh` の内容を実行。blossoML の deploy.yml と同じ方式）:
   1. `SHA256SUMS` を検証
   2. `releases/<version>/` に展開
   3. wheelhouse から `--no-index` で `venv/` を作成
   4. **自己診断** `python -m blossoml_ops.main selfcheck` を実行: 設定の読み込み、`rules.dfa.json` と `rules.yaml` のハッシュ照合、ツール許可リストと LTL の命題の整合、組み込みツールが無効になっていることの確認。**API は呼ばない、通知も送らない。**
   5. 自己診断が通ったら、`current` のシンボリックリンクを新しい版に原子的に切り替える（一時リンクを作って `mv -T`）
   6. 古いリリースは直近 3 つを残して削除する
7. 失敗した場合は `current` を切り替えずに終了する（旧版が動き続ける）。

**ロールバック**

`current` を 1 つ前のリリースに付け替えるだけ。手順を `deploy/` の README に書く（フェーズ 2 の `rollback_binary` は blossoML 本体のロールバックであり、これとは別物）。

**GitHub の secrets**

blossoML と同じく SSH 鍵・ホスト・ユーザーを secrets に置く。blossoML の secrets を流用するか、ops 用に別の鍵を作るかは運営者が決める（別の鍵にし、`authorized_keys` で接続元や実行内容を制限するのが望ましい）。

---

## 8. blossoML 本体への変更（エージェント導入の前提）

blossoML リポジトリへの PR として行う。コミットメッセージは Conventional 形式、1 行（リポジトリの CLAUDE.md に従う）。

**必須**

1. `lib/shell/storage_eio.ml`: blob ファイル・一時ファイル・空ファイルの作成パーミッションを `0o644` から `0o640` に変更する（`with_open_out ~create:(\`Or_truncate 0o644)` の箇所すべて）。
2. 本番の既存ファイルに一括で `chmod 640` を適用する手順を、`deploy/` の手順書に含める（PR ではなく運用手順）。
3. systemd ユニットに `UMask=0027` を設定する手順を含める。

**任意（あるとエージェントが楽になる）**

- `GET /health`（DB 接続と `data/` への書き込み可否を返す）
- ログの構造化（1 行 1 JSON）
- デプロイ時の DB バックアップ

---

## 9. フェーズと受け入れ条件

### フェーズ 0: 準備

- [ ] 本番で `uname -m` を確認
- [ ] blossoML の PR（8. の必須 1）をマージ、本番の既存ファイルに `chmod 640`
- [ ] bot 用 Nostr 鍵ペアの生成、kind 0 プロフィール設定
- [ ] 運営者の kind 10050 に 4 リレーのいずれかが含まれることを確認
- [ ] テスト用 blob（無害な短いテキスト）を運営者がアップロードし、sha256 を設定に記入

### フェーズ 1: 読み取り専用エージェント ＋ ltlguard コア

ltlguard:
- [ ] 開発環境に Spot（2.14 以上、Python バインディングつき）を導入し、手順を README に記載
- [ ] `ltlguard compile`: rules.yaml → Spot で DFA 変換 → 前計算（accepting / dead / safe_next）→ rules.dfa.json
- [ ] ランタイム: rules.dfa.json の読み込み、ガード式の評価器、rules.yaml とのハッシュ照合（Spot を import しない）
- [ ] 差分テスト: ランダムな短いトレースで、Spot の判定とランタイムのモニタの判定が一致すること
- [ ] 実行時モニタ（Pre で試算、Post で確定、4 値判定、fail-closed）
- [ ] SDK アダプタ（PreToolUse / PostToolUse、拒否メッセージ生成、終了時の義務チェック）
- [ ] トレース JSONL 出力
- [ ] 単体テスト: 各テンプレートの真理値、dead 判定、safe_next 計算、fail-closed

blossoml-ops:
- [ ] フェーズ 1 のツール 8 個（6.2）と出口検査（6.3）
- [ ] NIP-17 通知（6.4）。NIP-44 公式テストベクタを通すこと
- [ ] ルール R0, R6, R5', R8 を強制
- [ ] daily / postdeploy / manual モード
- [ ] 本番セットアップ一式（7.1, 7.3, 7.4, 7.5, 7.6。sudoers はまだ不要）
- [ ] CI のデプロイワークフロー、`remote-install.sh`、`selfcheck`、ロールバック手順（7.7）

受け入れ条件:
- [ ] **I1 のテスト**: 出口検査に画像・base64 画像・バイナリを入れると必ず拒否される。`blossoml-ops` ユーザーで blob ファイルを `open()` すると `PermissionError` になる（実機）。
- [ ] **I2 のテスト**: Bash / Read / WebFetch 等の組み込みツールを呼ぶよう誘導するプロンプトを与えても、呼び出しが拒否されトレースに `deny` が残る。
- [ ] **I4 のテスト**: `blossoml-ops` ユーザーで許可リスト外のドメインへ接続すると失敗する（実機）。
- [ ] daily モードを本番で 1 回実行し、DM が運営者のクライアントに届く。
- [ ] **メモリ実測**: 実行中の blossoML とエージェントのピークメモリを記録し、同居を続けるか、エージェントを Always Free の ARM インスタンスへ移すかを運営者が判断できる資料を残す。

### フェーズ 2: 書き込み系 ＋ 承認

- [ ] 書き込み系ツール（6.2）、root 所有スクリプト、sudoers
- [ ] 結果で決まる命題（`probe_ok`, `backup_ok`, `approved_*`）
- [ ] 承認フロー（6.5）
- [ ] ルール R1〜R5 を強制
- [ ] 終了時の差し戻し

### フェーズ 3: 評価モード

- [ ] トレース再生と指標（5.7）、影モード

### フェーズ 4: 汎用化と公開

- [ ] 2 つ目の用途（Claude Code 本体の hook でコーディング作業にルールを課す、など）で試す
- [ ] パラメータ付きルール（「ファイルごとに」等）
- [ ] ltlguard を別リポジトリに切り出して公開

---

## 10. 実装時に確認すること（設計時点で未確認）

| 項目 | 内容 |
|---|---|
| SDK: 組み込みツールの除去方法 | 組み込みツール一式を外すオプションの有無と名前。`disallowed_tools` と権限モードの正確な挙動 |
| SDK: 終了の差し戻し | Stop 系 hook で終了を差し戻せるか。できなければハーネスのループで代替（5.4） |
| SDK: subagent | Task / Agent ツールを無効化すれば subagent は起動しないことの確認 |
| SDK: hook の失敗時 | hook が例外を投げた・タイムアウトした場合の既定動作（fail-closed になるよう明示的に実装する） |
| SDK: プロキシ | Claude Code CLI サブプロセスが `HTTPS_PROXY` に従うか |
| Spot: Python API | `ltlf_to_mtdfa` / `minimize_mtdfa` の Python バインディングでの名前と使い方、MTDFA から状態・遷移を列挙する方法。使いにくければ `from_ltlf` → `translate` → `to_finite` の経路にする |
| Spot: 受理の表現 | 遷移ベースの受理を、ltlguard の状態ベースの `accepting` に変換する方法（差分テストで検証） |
| Spot: 導入方法 | 開発機・CI での入れ方（Debian/Ubuntu パッケージ、conda、ソースビルドのどれか）。本番には入れない |
| CI: ビルド環境 | 本番と同じ CPU アーキテクチャ・Python マイナーバージョンのランナーを用意できるか。`claude-agent-sdk` の wheel が対象プラットフォームで提供されているか、Node.js が別途必要か |
| CI: secrets | blossoML の SSH 鍵を流用するか、ops 用の鍵を新設するか（運営者に確認） |
| SQLite | ACL で `r--` のみの DB を `mode=ro` で開けるか（ジャーナル・WAL の扱い） |
| journald | python-systemd が本番の Python に入るか。入らない場合は `journalctl -o json` を固定 argv で呼ぶ |
| メモリ | Claude Code CLI ＋ Python のピーク使用量 |
| Nostr | 各リレーの NIP-42 要求・レート制限・gift wrap（kind 1059）の受理可否 |
| 公開鍵のマスク | ログ中の公開鍵を API に送る前にマスクするか（運営者に確認） |
| daily の実行時刻 | 運営者に確認 |
