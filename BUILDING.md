# Lumo のビルド方法

Thorium のビルド手順を元にしています。基本の流れは Thorium と同じです。
**ビルドは自分の PC ではなく GitHub Actions で行います。** ここに書いてある
ローカル手順は、動作確認や開発用です。

## 前提条件（ビルドマシン）

Thorium の `docs/BUILDING_WIN.md` は、Visual Studio / Windows SDK のバージョンは
Chromium の公式ドキュメント（`windows_build_instructions.md`）に従うとしています。
Chromium 公式の要件は次の通りです。

- 64bit マシン、RAM 最低 8GB・推奨 16GB 以上
- 空きディスク 100GB 以上（NTFS。ソースだけで 35〜40GBあります）
- Visual Studio 2022（Chromium が求めるバージョン）と Windows SDK
- Git、Python 3.11 以上
- `depot_tools`（Chromium のツール集）

**せあの PC はビルドしません。** GitHub Actions の `windows-2025` ランナーが
ビルドします。せあの PC に必要なのは、できあがった `lumo.exe` を動かすだけです
（Thorium の実行要件は RAM 4GB 以上・数百 MB の空き程度）。

## 1. ソースの取得（CI がやること）

```powershell
# Thorium のリポジトリをクローン（--recursive）
git clone --recursive https://github.com/Alex313031/thorium.git thorium-ref

# depot_tools を用意して PATH に追加
git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git

# Chromium を取得（fetch + gclient sync）。これだけで数十 GB・数時間かかります。
# Thorium の get_repo.py はこの処理を自動化しています。
#   fetch --nohooks chromium
#   gclient sync --nohooks
#   gclient runhooks
```

このリポジトリ（Lumo-Browser）には Chromium のソースは含めません。
CI の中で取得します。

## 2. Thorium のバージョン固定とパッチ適用

```powershell
py -3.11 version.py   # Thorium がピン留めした Chromium タグにチェックアウト
py -3.11 setup.py     # オーバーレイ・パッチ・翻訳を適用（--avx2 / --sse3 など）
```

`setup.py` のプロファイル（`--avx2` / `--sse3` / `--sse2` / `--woa` / `--avx512`）
と、`args.gn` の `thorium_x86_profile` は一致させる必要があります。
Lumo では最初は AVX2 版と SSE3 版のどちらか一方に絞ります（→ `PLAN.md`）。

## 3. GN 設定

Chromium の `src` ディレクトリで：

```powershell
gn args out/lumo
```

`args.gn.example` をコピーして使います。Windows x64 Release 用で、
Thorium の `win_args.gn`（Chromium 156 / Thorium 156.0.8078.80 時点）を元にしています。

```powershell
copy args.gn.example out/lumo/args.gn
gn args out/lumo --list   # 設定の確認
```

### Google API キー（Sync 用）

`args.gn.example` の次の 3 項目は**空欄のまま**です。絶対に本物のキーを
リポジトリにコミットしないでください。

```gn
google_api_key = ""
google_default_client_id = ""
google_default_client_secret = ""
```

本物のキーは GitHub の Secrets（`GOOGLE_API_KEY` / `GOOGLE_DEFAULT_CLIENT_ID` /
`GOOGLE_DEFAULT_CLIENT_SECRET`）に入れ、CI がビルド時に `args.gn` へ注入します。

キーの作り方（Chromium 公式「API Keys」手順の概要）：

1. `chromium-dev` グループと `google-browser-signin-testaccounts` グループに参加する
2. Google Cloud Console でプロジェクトを作り、必要な API を有効化する
3. OAuth クライアント ID（デスクトップアプリ）を作成し、クライアント ID と
   シークレット、API キーを取得する
4. 取得した 3 つの値を GitHub リポジトリの Secrets に登録する

## 4. ビルド

```powershell
py -3.11 build.py --expect-os win --expect-cpu x64
```

できあがりは `out/lumo/lumo.exe` と、プロファイル名付きのミニインストーラです。
（Thorium では `out/thorium/thorium.exe` でした。Lumo では `lumo` に置き換えます）

## 5. GitHub Actions でのビルド

`.github/workflows/build-windows.yml` を見てください。
Helium 式の 15 段階ジョブで、6 時間制限を回避しながらビルドします。
手動実行（`workflow_dispatch`）で、ランナーは `windows-2025`、タグ `v*` で
Release を作ります。DRAFT・未実行です。

## 参考

- Thorium `docs/BUILDING_WIN.md` / `docs/BUILDING.md` / `docs/ABOUT_GN_ARGS.md`
- Chromium 公式：`docs/windows_build_instructions.md`、`developers/how-tos/api-keys`
- Helium-windows `.github/workflows/build-steps.yml` / `build-stage.yml`
