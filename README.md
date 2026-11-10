# Lumo — Thorium式 Chromium フォーク

> **DRAFT / 未検証：** このリポジトリはまだ一度もビルドされていません。GitHubリポジトリの作成も、pushもしていません。

Chromium 本体をソースからビルドする、Thorium 式のブラウザです。
CEF（部品だけ使う方法）ではなく、Chromium のブラウザそのものをフォークして作ります。
だから Chrome と同じように拡張機能・Widevine・全コーデック・Google Sync（自分の API キー使用時）が使える設計です。

## なぜ Thorium 式か

- CEF では Chrome 拡張機能が使えず、Google Sync の仕組み自体が入っていません。
- Thorium は Chromium 本体をビルドするフォークで、拡張機能・Widevine・独自コーデック・
  Google API キーによる Sync を備えています。本プロジェクトはそのやり方を踏襲します。
- 土台は Thorium のリポジトリ構成とビルド手順（`get_repo.py` / `version.py` / `setup.py` /
  `build.py` / `win_args.gn`）を参考にしています。Thorium のソースコードそのものは
  このリポジトリには含めていません。

## 対象

- Windows 10/11 64bit（x64）を最初に作ります。AVX2 版と SSE3 版のどちらを出すかは未定です（→ `PLAN.md`）。
- ビルドは**自分の PC ではやりません**。GitHub Actions（クラウド）のサーバーでビルドします。
  せあの PC は、できあがったブラウザを動かすだけです。

## ビルドの仕組み（GitHub Actions）

Chromium のフルビルドは 10 時間以上かかることがあり、GitHub Actions の 1 ジョブは
最大 6 時間までしか動きません。そのため Helium がやっているのと同じ方法を取ります。

- ビルドを `build-1` 〜 `build-15` の連続ジョブに分けます。
- 各ジョブは終わらなかったらビルド途中の状態を zip にして Artifact として保存し、
  次のジョブがそれをダウンロードして続きから再開します（`finished == 'false'` の間だけ次が動く）。
- 全部終わったら `build-final` でパッケージ化し、タグ `v*` で `publish-release` が
  GitHub Release を作ります。
- 詳しくは [.github/workflows/build-windows.yml](.github/workflows/build-windows.yml)（DRAFT・未実行）を見てください。

## Google Sync について

- Sync を使うには、**提案者自身の Google API キー**が必要です。
- キーはリポジトリには絶対に入れません。GitHub の Secrets（`GOOGLE_API_KEY` /
  `GOOGLE_DEFAULT_CLIENT_ID` / `GOOGLE_DEFAULT_CLIENT_SECRET`）に入れ、
  ビルド時にだけ `args.gn` に注入します。`args.gn.example` の該当欄は空欄のままです。
- キーの作り方は Chromium 公式の「API Keys」手順に従います。
  （Chromium-dev グループへの参加などが必要です。詳しくは `BUILDING.md` を参照）
- 注意：Thorium の FAQ によると、Android では API キーがあっても Google Sync の
  サインインは Google 側の認可で失敗します。Sync はデスクトップ版の話です。
- また、Thorium の公開リポジトリには Thorium 自身のキーを埋め込むパッチがありますが、
  **本プロジェクトでは Thorium のキーを流用しません。** 自分のキーを使います。

## ライセンス

Lumo 自身はプロプライエタリ（All Rights Reserved）です。使用・複製・
再配布は禁止です。詳しくは `LICENSE.md` を見てください。

このリポジトリはビルドのためだけに一時的に公開しています。ビルドが
成功したら非公開に切り替えます。公開期間中も、Lumo を使う許可を
与えるものではありません。

ただし土台の Thorium・Chromium は BSD-3-Clause のままで、その部分の
ライセンスは変わりません。

## リポジトリの構成

| ファイル | 内容 |
|---|---|
| `README.md` | このファイル |
| `BUILDING.md` | ビルド手順（日本語） |
| `PLAN.md` | 未決定事項とリスクの一覧 |
| `args.gn.example` | Windows x64 Release 用 GN 引数のひな形（API キーは空欄） |
| `.github/workflows/build-windows.yml` | Helium 式の反復ビルドワークフロー（DRAFT） |
| `scripts/ci/stage.py` | 1 ジョブ分のビルドを実行するスクリプト（DRAFT・未検証） |

Chromium のソースコード（35〜40GB）はリポジトリには含めません。
CI の中で `depot_tools` + `gclient` を使って取得します。

## まだ決まっていないこと

`PLAN.md` を見てください。大きなものは次の通りです。

- ブラウザ名・リポジトリ名（Lumo / Lumo-Browser に決定済み）
- ピン留めする Chromium のバージョン（Thorium の追従バージョンに合わせるか）
- AVX2 版か SSE3 版か（せあの PC の CPU に合わせる）
- Thorium を直接フォークするか、素の Chromium + Thorium のパッチ集で作るか

## 参考にしたもの

- Thorium（Alex313031/thorium）— ビルド手順・GN 引数・パッチ構成。現行版は Thorium 156.0.8078.80（Chromium 156）。
- Helium-windows（imputnet/helium-windows）— 15 段階の反復ビルドワークフローと Artifact による再開の仕組み。
