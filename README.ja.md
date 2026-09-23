# MemoryPalace 2.0 · 作業メモリ

[中文](README.md) · [English](README.en.md) · **日本語**

MemoryPalace は、ツールや共同作業アプリケーションのために、出典を追跡できる記憶を保存します。メッセージ、操作結果、文書をソースとして受け取り、改訂可能な出来事や知識を記録し、範囲とトークン予算を指定した検索で関連情報を返します。Python パッケージと CLI の名前は `eventmem` です。

セッションをまたぐ作業の継続に役立ちます。確認済みの進捗、未確認の結果、約束、手順、再開時の入口を残せます。タスクの実行と記憶を読むタイミングはホストが決めます。MemoryPalace 自体はエージェントを実行せず、利用者に自発的なメッセージを送りません。

## 主な機能

- **ソースと改訂**：安定したソース ID、内容のスナップショット、引用、改訂履歴。現在の検索では訂正、撤回、アーカイブを再確認します。
- **作業の継続**：出来事、要約、チェックポイント、約束、手順は同じソースと記録の流れを使います。セッション境界には確認済みの進捗、未確定事項、次の手順を保存できます。
- **上限付き検索**：プロジェクト、コレクション、時刻、トークン予算で絞り込みます。完全一致と SQLite の全文検索はモデルなしで動き、ベクトルと関係検索は任意です。
- **永続的な処理**：SQLite が実行時の唯一の正本です。バックグラウンドジョブ、キャッシュの無効化、再試行を永続化し、索引は再構築できます。
- **接続方法**：ローカル HTTP サービス、MCP、Python／TypeScript SDK、CLI、ブラウザー管理画面。Codex、Claude Code、DeepSeek Harness との連携はサービスを使用し、オフラインの観測をローカルに一時保存します。
- **任意の機能**：文書・メディア解析、埋め込み、グラフ、明示的なリマインダー、ホストへのコールバック。基本インストールにモデルや個人設定、外部サービスは不要です。

## クイックスタート

Python 3.10 以降が必要です。リポジトリの開発と管理画面のビルドには Node.js 22 と [uv](https://docs.astral.sh/uv/) を使います。ビルド済み wheel のインストールに Node.js は不要です。

```sh
git clone https://github.com/mycyg/memory-palace.git
cd memory-palace
uv sync --frozen
uv run eventmem serve --root ./example-data
```

別のターミナルで合成の作業記録を書き込み、検索します。

```sh
uv run eventmem receive --root ./example-data --json '{"namespace":"demo","key":"rollback-1","occurred_at":"2026-09-01T00:00:00Z","scope":{"project":"demo"},"kind":"procedure","authority":"operation","text":"Rollback uses the last verified artifact."}'
uv run eventmem recall --root ./example-data --json '{"query":"rollback artifact","scope":{"project":"demo"},"scenario":"tool","budget":500}'
```

`eventmem console --root ./example-data` は `serve` の代わりにサービスを起動し、管理画面を開きます。サービスの既定アドレスは `127.0.0.1:8319`、データの既定ディレクトリは `~/.memorypalace` です。`--root` で独立したディレクトリを指定できます。CLI はローカルのデータを直接扱えます。HTTP と SDK には稼働中のサービスが必要です。

## 境界

元のソースとモデルが生成した要約は、異なる証拠ラベルを保持します。モデル設定は選択したバックグラウンド機能にのみ必要です。ソースの受付、改訂、ローカル検索はモデルなしで利用できます。リマインダーは予定とコールバックの結果を記録し、宛先、チャネルの権限、配信はホストが管理します。

2.0 では公開契約が変更され、一部の 1.x 連携との互換性がありません。既存データをバックアップし、**別の空ディレクトリ**へ移行してください。元のデータは変更されません。詳しくは[インストールと移行](docs/operations.md)を参照してください。

[現行アーキテクチャ](docs/architecture.md) · [連携と運用](docs/operations.md) · [作業の例](examples/README.md) · [作業リマインダー](docs/reminders.md) · [Codex 連携](docs/codex.md) · [2.0 合成ベンチマーク](docs/benchmarks/work-memory-v2.md) · [過去の 1.x 測定値](docs/performance.md)

[MIT License](LICENSE)
