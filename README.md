# Lumo — Thorium式 Chromium フォーク

Chromium 本体をソースからビルドする、Thorium 式のブラウザです。
CEF（部品だけ使う方法）ではなく、Chromium のブラウザそのものをフォークして作るため、
Chrome と同じように拡張機能・Widevine・全コーデック・Google Sync（自分の API キー使用時）が使えます。

ベースは Chromium 156.0.8078.80（Thorium のピン留めコミットに追従）です。

> このブラウザは AI によって作成されました。

## 状態

- Windows x64 版が GitHub Actions でビルドできており、オーナーの実機で日常利用中です。
- 成果物は GitHub Release ではなく、Actions の Artifact（`lumo-windows-x64.zip`）として受け取ります。
- `lumo.exe` は自己署名済みです（SmartScreen の警告は出ます）。

## Lumo の特徴

- **Google Sync**：オーナー自身の Google API キーで、サインインと同期が使えます。
- **起動時パスワードロック**：初回起動時のみパスワードを確認します（ハッシュはビルド時に Secrets から注入）。
- **Lumo Monitor**：設定内の専用ページで、バージョン・プロファイル保存先・稼働時間・Sync 状態などを確認できます。
- **Lumo Labs（試験運用機能）**：Lumo 独自の実験機能をまとめたパネルです。
- **日本語化**：chrome://flags の項目説明を日本語化し、DevTools の既定言語も日本語にしています。
- **lumo:// スキーム**：内部ページは chrome:// に加えて lumo:// でも開けます。
- **標準搭載の拡張機能**：uBlock Origin、uBO Scope、Chrome Info Viewer を新規プロファイルに自動で入れます。
- **chrome://tools**：ブラウザ内で使えるツール集のページです。
- **メモリセーバー拡張段階**：操作していないタブを約4分で破棄する段階を追加しています。
- ウィンドウ左上に「このブラウザーは試験的な開発段階です」と常時表示されます。
- ユーザーデータは `%LOCALAPPDATA%\Lumo_Browser\User Data` に保存され、Thorium や Chrome とは混ざりません。

## 対象とビルドの仕組み

- Windows 10/11 64bit（x64）、AVX2 版のみです（オーナーの PC は Intel Core i5-8350U、AVX2 対応確認済み。SSE3 版は作りません）。
- ビルドは自分の PC ではやりません。GitHub Actions（クラウド）で行います。
- Chromium のフルビルドは GitHub Actions の 1 ジョブ上限（6時間）を超えるため、
  `build-1` 〜 `build-15` の連続ジョブに分け、各ジョブがビルド途中の状態を
  チェックポイントとして Artifact に保存し、次のジョブが続きから再開します。
  全部終わったら最後のジョブがパッケージ化します。
- チェックポイントには秘密情報を含めません（API キー等は zip から除外し、作成後に混入検査をします。再開時に Secrets から再注入します）。
- 詳しくは [.github/workflows/build-windows.yml](.github/workflows/build-windows.yml) と `BUILDING.md` を見てください。

## Google Sync について

- Sync を使うには、**オーナー自身の Google API キー**が必要です。
- キーはリポジトリには絶対に入れません。GitHub の Secrets（`GOOGLE_API_KEY` /
  `GOOGLE_DEFAULT_CLIENT_ID` / `GOOGLE_DEFAULT_CLIENT_SECRET`）に入れ、
  ビルド時にだけ注入します。`args.gn.example` の該当欄は空欄のままです。
- キーの作り方は Chromium 公式の「API Keys」手順に従います（詳しくは `BUILDING.md` を参照）。
- Thorium のキーは流用しません。自分のキーを使います。

## ライセンス

Lumo 自身はプロプライエタリ（All Rights Reserved）です。使用・複製・
再配布は禁止です。詳しくは `LICENSE.md` を見てください。

このリポジトリはビルドのためだけに一時的に公開しています。ビルドが
安定したら非公開に切り替える予定です。公開期間中も、Lumo を使う許可を
与えるものではありません。

ただし土台の Thorium・Chromium は BSD-3-Clause のままで、その部分の
ライセンスは変わりません。

## リポジトリの構成

| ファイル | 内容 |
|---|---|
| `README.md` | このファイル |
| `BUILDING.md` | ビルド手順（日本語） |
| `PLAN.md` | 未決定事項とリスクの一覧 |
| `LICENSE.md` | ライセンス（Lumo 独自部分は All Rights Reserved） |
| `args.gn.example` | Windows x64 Release 用 GN 引数のひな形（API キーは空欄） |
| `.github/workflows/` | ビルド用ワークフロー（呼び出し元とステージ実行） |
| `scripts/ci/stage.py` | 1 ジョブ分のビルドを実行するスクリプト |
| `scripts/ci/lumo_patches/` | Lumo 独自パッチ集 |
| `scripts/ci/lumo_branding/` | Lumo のブランド素材（アイコン等） |
| `scripts/ci/lumo_extensions/` | 標準搭載する拡張機能の同梱物 |
| `scripts/ci/lumo_flag_translations.json` | chrome://flags の日本語対応表 |

Chromium のソースコード（35〜40GB）はリポジトリには含めません。
CI の中で取得します。

## Android 版

Android 版は別プロジェクトとして、microG 版 ungoogled-chromium を土台に
別リポジトリで開発中です。

## 参考にしたもの

- Thorium（Alex313031/thorium）— ビルド手順・GN 引数・パッチ構成。現行版は Thorium 156.0.8078.80（Chromium 156）。
- Helium-windows（imputnet/helium-windows）— 15 段階の反復ビルドワークフローと Artifact による再開の仕組み。
