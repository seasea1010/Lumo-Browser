# Lumo — 未決定事項とリスク

## 決まったこと（2026-11-10）

- ブラウザ名：**Lumo**、リポジトリ名：**Lumo-Browser**、**公開リポジトリ**
- 方式：Thorium 式の Chromium フォーク（Chromium 本体をソースからビルド）
- 対象：Windows 10/11 x64 が最初
- ビルド：自分の PC ではなく GitHub Actions（クラウド）で行う
- 公開にした理由：非公開だと Actions の無料枠が月 2,000 分で足りなくなるため。
  公開リポジトリでは標準ランナーが無料・無制限になる。

## 未決定事項

1. **Thorium を直接フォークするか、素の Chromium + Thorium のパッチ集で作るか**
   - 直接フォーク：Thorium の `get_repo.py` / `version.py` / `setup.py` / `build.py` /
     パッチ集がそのまま使えて一番早い。ただしブランド名（thorium → lumo）の
     置き換えと、Thorium 固有の既定値の見直しが必要。
   - 素の Chromium + パッチ：クリーンだが、Thorium のパッチ適用順序や
     オーバーレイの再現が大変。最初は直接フォークが現実的。
2. **ピン留めする Chromium のバージョン**
   - 調査時点の Thorium は 156.0.8078.80（Chromium 156）。追従するバージョンを
     リポジトリ作成時に改めて確認して固定する。
3. **AVX2 版か SSE3 版か（あるいは両方か）**
   - Thorium は AVX2 / AVX / SSE3 などの CPU 別ビルドを出している。
   - せあの PC の CPU が AVX2 に対応しているか確認して決める。両方出すと
     ビルド時間が 2 倍になる。
4. **Google API キーの取得**
   - せあ自身のキーが必要。Chromium-dev グループ参加、Google Cloud Console での
     プロジェクト作成、OAuth クライアント作成、Secrets への登録が必要。
   - キーがなくてもブラウザ自体はビルドできる（Sync だけ使えない）。
5. **ブランディング**
   - 実行ファイル名は `lumo.exe`、製品名は Lumo。アイコン・新しいタブページ・
     About ページの文言は未作成。

## リスク

- **初回ビルドの時間**：Chromium のフルビルドは 10 時間以上かかることがある。
  Helium 式の 15 段階ジョブで分割するが、初めて成功するまで何日もかかる可能性がある。
  途中で失敗したときの切り分けも大変。
- **Actions の Artifact サイズ制限**：ビルド途中の状態を zip（数十 GB になる可能性あり）
  で Artifact に保存して次のジョブへ渡す。GitHub の Artifact にはサイズ・保存期間の
  制限があり、大きすぎるとアップロードに失敗する。Helium は 7z で圧縮して対処している。
  場合によってはキャッシュ（sccache）併用や、有料の大型ランナーが必要になる。
- **API キーと Sync の脆さ**：Google は 2021 年に非公式 Chromium の Sync を制限した。
  自分のキーで動くはずだが、Google 側のポリシー変更でいつ使えなくなるか分からない。
  Thorium の FAQ でも、Android では API キーがあっても Sync サインインは失敗すると
  されている。デスクトップ版が対象。
- **Thorium のキーの流用はしない**：Thorium の公開リポジトリには Thorium 自身の
  API キーを埋め込むパッチ（`other/thorium-default-api-keys.patch`）があるが、
  他人のキーを使うのは規約上も筋が悪いので、Lumo では使わない。自分のキーを使う。
- **Widevine / HEVC のライセンス**：Thorium は Widevine CDM や HEVC 対応を
  有効化しているが、再配布には別途ライセンスの確認が必要になる場合がある。
  公開リポジトリで配布する以上、配布物のライセンス表示を整えること。
- **Chromium の更新頻度**：Chromium は約 4 週間ごとにメジャーバージョンが上がる。
  追従しないとセキュリティ修正が古くなる。更新作業（rebase・パッチ当て直し）は
  継続コストになる。
