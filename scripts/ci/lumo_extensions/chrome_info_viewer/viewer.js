'use strict';

/* ========== ヘルパー ========== */
const NA = '取得できません';
const $ = (s, r = document) => r.querySelector(s);
const bytes = n => {
  if (typeof n !== 'number' || isNaN(n)) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 2 : 0) + ' ' + u[i];
};
const yn = b => (b ? 'はい' : 'いいえ');
const ok = b => (b ? '✅ 対応' : '❌ 非対応');
const mq = q => matchMedia(q).matches;
const ctrl = {
  not_controllable: '変更不可',
  controlled_by_other_extensions: '他の拡張機能が制御',
  controllable_by_this_extension: 'この拡張機能が変更可能',
  controlled_by_this_extension: 'この拡張機能が制御中'
};
const P = api => async () => {
  const r = await api.get({});
  const v = typeof r.value === 'boolean' ? yn(r.value) : String(r.value);
  return `${v}（${ctrl[r.levelOfControl] || r.levelOfControl}）`;
};

let _hev, _gl;
const hev = () => (_hev ??= navigator.userAgentData.getHighEntropyValues(
  ['architecture', 'bitness', 'model', 'platformVersion', 'uaFullVersion', 'fullVersionList', 'wow64', 'formFactors']));
const getGL = () => {
  if (_gl === undefined) {
    const c = document.createElement('canvas');
    _gl = c.getContext('webgl2') || c.getContext('webgl') || null;
  }
  return _gl;
};
const glRenderer = which => {
  const g = getGL(); const e = g.getExtension('WEBGL_debug_renderer_info');
  if (!e) return g.getParameter(which === 'v' ? g.VENDOR : g.RENDERER);
  return g.getParameter(which === 'v' ? e.UNMASKED_VENDOR_WEBGL : e.UNMASKED_RENDERER_WEBGL);
};
const countBookmarks = nodes => nodes.reduce((n, x) => n + (x.url ? 1 : 0) + (x.children ? countBookmarks(x.children) : 0), 0);
let _cpuPrev = null, _cpuP = null, _cpuT = 0;
async function cpuCompute() {
  const p = (await chrome.system.cpu.getInfo()).processors.map(x => x.usage);
  let res = null;
  if (_cpuPrev) {
    const per = p.map((u, i) => { const dt = u.total - _cpuPrev[i].total; return dt > 0 ? Math.round((1 - (u.idle - _cpuPrev[i].idle) / dt) * 100) : 0; });
    const tot = p.reduce((a, u) => a + u.total, 0) - _cpuPrev.reduce((a, u) => a + u.total, 0);
    const idl = p.reduce((a, u) => a + u.idle, 0) - _cpuPrev.reduce((a, u) => a + u.idle, 0);
    res = { all: tot > 0 ? Math.round((1 - idl / tot) * 100) : 0, per };
  }
  _cpuPrev = p; return res;
}
const cpuUsage = () => { if (_cpuP && Date.now() - _cpuT < 400) return _cpuP; _cpuT = Date.now(); return (_cpuP = cpuCompute()); };
const when = t => (t ? new Date(t).toLocaleString() : '—');

/* ========== セクション定義 ========== */
// items: [ラベル, 値 or 関数, 補足説明]   tables: {title, cols, rows: 関数}
const SECTIONS = [
  {
    id: 'browser', icon: '🌐', title: 'ブラウザ', desc: 'Chrome本体とOSの基本情報です。',
    events: t => { addEventListener('online', t); addEventListener('offline', t); addEventListener('languagechange', t); },
    items: [
      ['Chromeバージョン', async () => {
        const h = await hev(); const l = h.fullVersionList || [];
        const b = l.find(x => x.brand === 'Google Chrome') || l.find(x => x.brand === 'Chromium');
        return b ? `${b.brand} ${b.version}` : navigator.userAgent;
      }, '正確なバージョン番号'],
      ['ブランド一覧', () => navigator.userAgentData.brands.map(b => `${b.brand} ${b.version}`), 'Chromiumベースのブラウザ名'],
      ['OS', async () => {
        const h = await hev(); let s = `${h.platform} ${h.platformVersion}`;
        if (h.platform === 'Windows') s += parseInt(h.platformVersion) >= 13 ? '（Windows 11 以降）' : '（Windows 10 以前）';
        return s;
      }, 'OSの種類とバージョン'],
      ['CPUアーキテクチャ', async () => { const h = await hev(); return `${h.architecture} / ${h.bitness}bit`; }],
      ['機種（モデル）', async () => (await hev()).model || '（なし）', 'スマホ等で表示。PCは通常空'],
      ['モバイル端末', () => yn(navigator.userAgentData.mobile)],
      ['フォームファクター', async () => (await hev()).formFactors, 'Desktop / Mobile / Tablet など'],
      ['ユーザーエージェント', () => navigator.userAgent, 'サイトに送られる自己紹介文字列'],
      ['言語（優先）', () => navigator.language],
      ['言語リスト', () => navigator.languages, '優先順'],
      ['Chrome UI言語', () => chrome.i18n.getUILanguage()],
      ['Accept-Language', () => chrome.i18n.getAcceptLanguages(), 'Webサイトに伝える受け入れ言語'],
      ['OS名 / CPU', async () => { const p = await chrome.runtime.getPlatformInfo(); return `${p.os} / ${p.arch}`; }],
      ['Cookie有効', () => yn(navigator.cookieEnabled)],
      ['Do Not Track', () => navigator.doNotTrack ?? '未設定'],
      ['自動操作(webdriver)', () => yn(navigator.webdriver), '自動化ツールで操作されているか'],
      ['PDFビューア内蔵', () => yn(navigator.pdfViewerEnabled)],
      ['オンライン', () => yn(navigator.onLine)],
      ['この拡張機能のID', () => chrome.runtime.id],
      ['この拡張機能のバージョン', () => chrome.runtime.getManifest().version]
    ]
  },
  {
    id: 'hardware', icon: '💻', title: 'PC・ハードウェア', desc: 'CPU・メモリ・GPU・バッテリーなど。',
    events: t => {
      navigator.getBattery?.().then(b => ['levelchange', 'chargingchange', 'chargingtimechange', 'dischargingtimechange'].forEach(e => b.addEventListener(e, t))).catch(() => {});
      addEventListener('gamepadconnected', t); addEventListener('gamepaddisconnected', t);
      chrome.system.storage.onAttached.addListener(t); chrome.system.storage.onDetached.addListener(t);
    },
    items: [
      ['論理CPUコア数', () => navigator.hardwareConcurrency, 'スレッド数'],
      ['CPUモデル', async () => (await chrome.system.cpu.getInfo()).modelName],
      ['CPUアーキテクチャ名', async () => (await chrome.system.cpu.getInfo()).archName],
      ['CPU対応機能', async () => (await chrome.system.cpu.getInfo()).features, 'SSE / AVX など'],
      ['CPU使用率（全体）', async () => { const r = await cpuUsage(); return r ? `${r.all}%` : '計測中…'; }, '1秒ごとに更新', 1000],
      ['CPU使用率（コア別）', async () => { const r = await cpuUsage(); return r ? r.per.map((u, i) => `コア${i}: ${u}%`) : '計測中…'; }, '', 1000],
      ['メモリ（正確）', async () => {
        const m = await chrome.system.memory.getInfo();
        return `合計 ${bytes(m.capacity)}（空き ${bytes(m.availableCapacity)}）`;
      }, '空き容量を2秒ごとに更新', 2000],
      ['メモリ（ブラウザ申告）', () => `${navigator.deviceMemory} GB以上`, 'プライバシー保護のため丸められた値'],
      ['GPUメーカー', () => glRenderer('v')],
      ['GPU名', () => glRenderer('r'), 'WebGL経由で取得'],
      ['WebGL バージョン', () => getGL().getParameter(getGL().VERSION)],
      ['WebGL 最大テクスチャ', () => getGL().getParameter(getGL().MAX_TEXTURE_SIZE) + ' px'],
      ['WebGPU アダプタ', async () => {
        const i = (await navigator.gpu.requestAdapter()).info;
        return [i.vendor, i.architecture, i.description].filter(Boolean).join(' / ');
      }],
      ['バッテリー', async () => {
        const b = await navigator.getBattery();
        const t = b.charging ? (isFinite(b.chargingTime) ? `・満充電まで約${Math.round(b.chargingTime / 60)}分` : '') : (isFinite(b.dischargingTime) ? `・残り約${Math.round(b.dischargingTime / 60)}分` : '');
        return `${Math.round(b.level * 100)}%（${b.charging ? '充電中' : '放電中'}${t}）`;
      }],
      ['タッチ最大同時点数', () => navigator.maxTouchPoints, '0ならタッチ非対応'],
      ['JSヒープ使用量', () => {
        const m = performance.memory; return `${bytes(m.usedJSHeapSize)} / 上限 ${bytes(m.jsHeapSizeLimit)}`;
      }, 'このページ自身のJSメモリ', 2000],
      ['接続中のゲームパッド', () => navigator.getGamepads().filter(Boolean).map(g => g.id)]
    ],
    tables: [{
      title: 'ストレージ（ドライブ）', cols: ['名前', '種類', '容量'],
      rows: async () => (await chrome.system.storage.getInfo()).map(s => [s.name, s.type, bytes(s.capacity)])
    }]
  },
  {
    id: 'display', icon: '🖥️', title: '画面・表示', desc: '解像度、拡大率、色、ダークモード設定など。', debounce: 100,
    events: t => {
      addEventListener('resize', t); screen.orientation?.addEventListener('change', t);
      ['(prefers-color-scheme: dark)', '(prefers-reduced-motion: reduce)', '(prefers-contrast: more)', '(forced-colors: active)', '(dynamic-range: high)', '(hover: hover)', '(pointer: fine)', '(pointer: coarse)']
        .forEach(q => matchMedia(q).addEventListener('change', t));
      chrome.system.display.onDisplayChanged.addListener(t);
    },
    items: [
      ['画面解像度', () => `${screen.width} × ${screen.height}`],
      ['作業領域', () => `${screen.availWidth} × ${screen.availHeight}`, 'タスクバーを除いた領域'],
      ['ウィンドウ内側', () => `${innerWidth} × ${innerHeight}`],
      ['ウィンドウ全体', () => `${outerWidth} × ${outerHeight}`],
      ['デバイスピクセル比', () => devicePixelRatio, '2なら高解像度(Retina)や拡大率200%'],
      ['色深度', () => `${screen.colorDepth} bit`],
      ['画面の向き', () => `${screen.orientation.type}（${screen.orientation.angle}°）`],
      ['複数ディスプレイ', () => yn(screen.isExtended)],
      ['HDR', () => yn(mq('(dynamic-range: high)'))],
      ['色域', () => mq('(color-gamut: rec2020)') ? 'Rec.2020' : mq('(color-gamut: p3)') ? 'Display P3' : 'sRGB'],
      ['ダークモード', () => yn(mq('(prefers-color-scheme: dark)'))],
      ['アニメーション削減', () => yn(mq('(prefers-reduced-motion: reduce)'))],
      ['コントラスト強調', () => yn(mq('(prefers-contrast: more)'))],
      ['強制カラー(高コントラスト)', () => yn(mq('(forced-colors: active)'))],
      ['ホバー可能', () => yn(mq('(hover: hover)'))],
      ['主入力デバイス', () => mq('(pointer: fine)') ? 'マウス等（精密）' : mq('(pointer: coarse)') ? 'タッチ（粗い）' : 'なし']
    ],
    tables: [{
      title: 'ディスプレイ一覧', cols: ['名前', '主画面', '解像度', 'DPI', '回転', '内蔵'],
      rows: async () => (await chrome.system.display.getInfo()).map(d =>
        [d.name || d.id, yn(d.isPrimary), `${d.bounds.width}×${d.bounds.height}`, `${d.dpiX}×${d.dpiY}`, d.rotation + '°', yn(d.isInternal)])
    }]
  },
  {
    id: 'network', icon: '📡', title: 'ネットワーク', desc: '回線状態とプロキシ設定です。', poll: 5000,
    events: t => {
      addEventListener('online', t); addEventListener('offline', t); navigator.connection?.addEventListener('change', t);
      chrome.proxy.settings.onChange.addListener(t); chrome.privacy.network.webRTCIPHandlingPolicy.onChange.addListener(t);
    },
    items: [
      ['オンライン', () => yn(navigator.onLine)],
      ['回線タイプ', () => navigator.connection.effectiveType, '4g / 3g など（推定）'],
      ['推定下り速度', () => navigator.connection.downlink + ' Mbps'],
      ['推定遅延(RTT)', () => navigator.connection.rtt + ' ms'],
      ['データセーバー', () => yn(navigator.connection.saveData)],
      ['公開IPアドレス', { btn: '取得する（ipify.orgに問い合わせ）', run: async () => (await (await fetch('https://api64.ipify.org?format=json')).json()).ip }, '外部サーバーへの通信が発生します'],
      ['プロキシ設定', async () => {
        const r = await chrome.proxy.settings.get({}); return `${r.value.mode}（${ctrl[r.levelOfControl] || r.levelOfControl}）`;
      }],
      ['WebRTC IP公開ポリシー', P(chrome.privacy.network.webRTCIPHandlingPolicy), 'IP漏れ対策の設定']
    ]
  },
  {
    id: 'storage', icon: '🗄️', title: 'ストレージ・地域', desc: '保存容量と、タイムゾーン・日付形式など。',
    items: [
      ['ブラウザ保存容量の上限', async () => bytes((await navigator.storage.estimate()).quota), '', 5000],
      ['使用中の容量', async () => bytes((await navigator.storage.estimate()).usage), '', 5000],
      ['永続化済み', async () => yn(await navigator.storage.persisted())],
      ['タイムゾーン', () => Intl.DateTimeFormat().resolvedOptions().timeZone, '', 5000],
      ['UTCとの差', () => `UTC${-new Date().getTimezoneOffset() / 60 >= 0 ? '+' : ''}${-new Date().getTimezoneOffset() / 60}`, '', 5000],
      ['ロケール', () => Intl.DateTimeFormat().resolvedOptions().locale],
      ['暦', () => Intl.DateTimeFormat().resolvedOptions().calendar],
      ['数字体系', () => Intl.DateTimeFormat().resolvedOptions().numberingSystem],
      ['数値表記の例', () => new Intl.NumberFormat().format(1234567.89)],
      ['現在時刻', () => new Date().toString(), '1秒ごとに更新', 1000]
    ]
  },
  {
    id: 'tabs', icon: '🗂️', title: 'タブ・ウィンドウ', desc: '今開いているタブの状況です。', debounce: 300,
    events: t => {
      ['onCreated', 'onRemoved', 'onUpdated', 'onActivated', 'onMoved', 'onAttached', 'onDetached', 'onReplaced'].forEach(e => chrome.tabs[e].addListener(t));
      ['onCreated', 'onRemoved'].forEach(e => chrome.windows[e].addListener(t));
      ['onCreated', 'onRemoved', 'onUpdated'].forEach(e => chrome.tabGroups[e].addListener(t));
    },
    items: [
      ['ウィンドウ数', async () => (await chrome.windows.getAll()).length],
      ['タブ総数', async () => (await chrome.tabs.query({})).length],
      ['タブグループ数', async () => (await chrome.tabGroups.query({})).length],
      ['固定タブ', async () => (await chrome.tabs.query({ pinned: true })).length],
      ['音が鳴っているタブ', async () => (await chrome.tabs.query({ audible: true })).length],
      ['スリープ中のタブ', async () => (await chrome.tabs.query({ discarded: true })).length, 'メモリ節約で休止中'],
      ['シークレット許可', () => chrome.extension.isAllowedIncognitoAccess().then(yn), 'この拡張機能がシークレットで動くか']
    ],
    tables: [{
      title: '開いているタブ一覧', cols: ['窓', 'タイトル', 'URL', '状態'],
      rows: async () => (await chrome.tabs.query({})).slice(0, 300).map(t =>
        [t.windowId, t.title, t.url || t.pendingUrl, [t.active ? '表示中' : '', t.pinned ? '📌' : '', t.audible ? '🔊' : '', t.discarded ? '💤' : '', t.incognito ? '🕶' : ''].filter(Boolean).join(' ')])
    }]
  },
  {
    id: 'extensions', icon: '🧩', title: '拡張機能・アプリ', desc: 'インストール済みの拡張機能一覧です。',
    events: t => ['onInstalled', 'onUninstalled', 'onEnabled', 'onDisabled'].forEach(e => chrome.management[e].addListener(t)),
    items: [
      ['インストール数', async () => (await chrome.management.getAll()).length],
      ['有効', async () => (await chrome.management.getAll()).filter(e => e.enabled).length],
      ['無効', async () => (await chrome.management.getAll()).filter(e => !e.enabled).length]
    ],
    tables: [{
      title: '拡張機能一覧', cols: ['名前', 'バージョン', '状態', '種類', '入手元', 'ID'],
      rows: async () => (await chrome.management.getAll()).sort((a, b) => b.enabled - a.enabled).map(e =>
        [e.name, e.version, e.enabled ? '✅ 有効' : '⛔ 無効', e.type, e.installType, e.id])
    }]
  },
  {
    id: 'profile', icon: '👤', title: 'プロフィール・データ量', desc: 'アカウント、ブックマーク・履歴・Cookieなどの件数。', debounce: 2000,
    events: t => {
      ['onCreated', 'onRemoved', 'onChanged', 'onMoved', 'onImportEnded'].forEach(e => chrome.bookmarks[e].addListener(t));
      chrome.history.onVisited.addListener(t); chrome.history.onVisitRemoved.addListener(t);
      chrome.cookies.onChanged.addListener(t);
      ['onCreated', 'onChanged', 'onErased'].forEach(e => chrome.downloads[e].addListener(t));
      chrome.sessions.onChanged.addListener(t); chrome.identity.onSignInChanged.addListener(t);
    },
    items: [
      ['ログイン中のGoogleアカウント', async () => (await chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' })).email || '未ログイン'],
      ['アカウントID', async () => (await chrome.identity.getProfileUserInfo({ accountStatus: 'ANY' })).id || '—'],
      ['ブックマーク数', async () => countBookmarks(await chrome.bookmarks.getTree())],
      ['履歴件数（最大10万）', async () => (await chrome.history.search({ text: '', startTime: 0, maxResults: 100000 })).length],
      ['Cookie数', async () => (await chrome.cookies.getAll({})).length],
      ['Cookieのあるドメイン数', async () => new Set((await chrome.cookies.getAll({})).map(c => c.domain.replace(/^\./, ''))).size],
      ['ダウンロード履歴数', async () => (await chrome.downloads.search({})).length]
    ],
    tables: [
      {
        title: 'よく見るサイト', cols: ['タイトル', 'URL'],
        rows: async () => (await chrome.topSites.get()).map(s => [s.title, s.url])
      },
      {
        title: 'Cookieが多いサイト TOP10', cols: ['ドメイン', '件数'],
        rows: async () => {
          const m = {}; (await chrome.cookies.getAll({})).forEach(c => { const d = c.domain.replace(/^\./, ''); m[d] = (m[d] || 0) + 1; });
          return Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, 10);
        }
      },
      {
        title: '最近閉じたタブ・ウィンドウ', cols: ['タイトル', 'URL / 内容'],
        rows: async () => (await chrome.sessions.getRecentlyClosed({ maxResults: 10 })).map(s =>
          s.tab ? [s.tab.title, s.tab.url] : ['（ウィンドウ）', `${s.window.tabs.length}タブ`])
      },
      {
        title: '最近のダウンロード', cols: ['ファイル', '状態', 'サイズ', '日時'],
        rows: async () => (await chrome.downloads.search({ orderBy: ['-startTime'], limit: 10 })).map(d =>
          [d.filename.split(/[\\/]/).pop(), d.state, bytes(d.totalBytes), when(d.startTime)])
      },
      {
        title: '同期中の他デバイス', cols: ['デバイス名', 'セッション数'],
        rows: async () => (await chrome.sessions.getDevices()).map(d => [d.deviceName, d.sessions.length])
      }
    ]
  },
  {
    id: 'privacy', icon: '🔒', title: 'プライバシー設定', desc: 'Chromeのプライバシー関連の設定値と、サイト権限の既定値。', poll: 3000,
    items: [
      ['ネットワーク予測', P(chrome.privacy.network.networkPredictionEnabled), '先読みの有効/無効'],
      ['サードパーティCookie許可', P(chrome.privacy.websites.thirdPartyCookiesAllowed)],
      ['ハイパーリンク監査', P(chrome.privacy.websites.hyperlinkAuditingEnabled)],
      ['Do Not Track送信', P(chrome.privacy.websites.doNotTrackEnabled)],
      ['リファラー送信', P(chrome.privacy.websites.referrersEnabled)],
      ['保護されたコンテンツ', P(chrome.privacy.websites.protectedContentEnabled)],
      ['セーフブラウジング', P(chrome.privacy.services.safeBrowsingEnabled)],
      ['翻訳機能', P(chrome.privacy.services.translationServiceEnabled)],
      ['自動入力（住所）', P(chrome.privacy.services.autofillAddressEnabled)],
      ['自動入力（カード）', P(chrome.privacy.services.autofillCreditCardEnabled)],
      ['パスワード保存', P(chrome.privacy.services.passwordSavingEnabled)],
      ...['cookies', 'javascript', 'images', 'popups', 'location', 'notifications', 'microphone', 'camera', 'automaticDownloads'].map(n =>
        [`サイト既定: ${{ cookies: 'Cookie', javascript: 'JavaScript', images: '画像', popups: 'ポップアップ', location: '位置情報', notifications: '通知', microphone: 'マイク', camera: 'カメラ', automaticDownloads: '自動ダウンロード' }[n]}`,
          async () => (await chrome.contentSettings[n].get({ primaryUrl: 'https://example.com' })).setting])
    ]
  },
  {
    id: 'permissions', icon: '🛂', title: '権限', desc: 'この拡張機能が持つ権限と、ブラウザAPIの許可状態。', poll: 3000,
    items: [
      ['拡張機能の権限', async () => (await chrome.permissions.getAll()).permissions],
      ['アクセス可能なサイト', async () => (await chrome.permissions.getAll()).origins],
      ...['geolocation', 'notifications', 'camera', 'microphone', 'clipboard-read', 'clipboard-write', 'midi', 'persistent-storage', 'push', 'background-sync', 'accelerometer', 'gyroscope', 'magnetometer', 'screen-wake-lock', 'window-management', 'local-fonts', 'idle-detection'].map(n =>
        [`API: ${n}`, async () => {
          const s = (await navigator.permissions.query({ name: n })).state;
          return { granted: '✅ 許可', denied: '⛔ 拒否', prompt: '❓ 確認する' }[s] || s;
        }])
    ]
  },
  {
    id: 'media', icon: '🎬', title: 'メディア・音声', desc: '再生できる形式、カメラ/マイク、読み上げ音声。',
    events: t => { navigator.mediaDevices.addEventListener('devicechange', t); speechSynthesis.addEventListener('voiceschanged', t); },
    items: [
      ['Widevine(DRM再生)', async () => {
        await navigator.requestMediaKeySystemAccess('com.widevine.alpha', [{ initDataTypes: ['cenc'], videoCapabilities: [{ contentType: 'video/mp4; codecs="avc1.42E01E"' }] }]);
        return '✅ 対応（Netflix等のDRM動画が再生可能）';
      }],
      ['カメラ数', async () => (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput').length],
      ['マイク数', async () => (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audioinput').length],
      ['スピーカー数', async () => (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'audiooutput').length],
      ['読み上げ音声の数', async () => (await chrome.tts.getVoices()).length]
    ],
    tables: [
      {
        title: '映像・音声コーデック対応', cols: ['形式', '再生'],
        rows: async () => [
          ['H.264 (AVC)', 'video/mp4; codecs="avc1.42E01E"'], ['H.265 (HEVC)', 'video/mp4; codecs="hvc1.1.6.L93.B0"'],
          ['VP8', 'video/webm; codecs="vp8"'], ['VP9', 'video/webm; codecs="vp9"'], ['AV1', 'video/mp4; codecs="av01.0.05M.08"'],
          ['AAC', 'audio/mp4; codecs="mp4a.40.2"'], ['MP3', 'audio/mpeg'], ['Opus', 'audio/webm; codecs="opus"'],
          ['Vorbis', 'audio/ogg; codecs="vorbis"'], ['FLAC', 'audio/flac']
        ].map(([n, t]) => [n, ok(MediaSource.isTypeSupported(t) || !!document.createElement('video').canPlayType(t))])
      },
      {
        title: '読み上げ音声一覧', cols: ['名前', '言語', '種類'],
        rows: async () => (await chrome.tts.getVoices()).map(v => [v.voiceName, v.lang || '—', v.remote ? 'オンライン' : 'ローカル'])
      }
    ]
  },
  {
    id: 'fonts', icon: '🔤', title: 'フォント', desc: 'インストールされているフォントです。',
    items: [
      ['フォント数', async () => (await chrome.fontSettings.getFontList()).length],
      ['フォント一覧', async () => (await chrome.fontSettings.getFontList()).map(f => f.displayName)],
      ['標準フォント', async () => (await chrome.fontSettings.getFont({ genericFamily: 'standard' })).fontId],
      ['等幅フォント', async () => (await chrome.fontSettings.getFont({ genericFamily: 'fixed' })).fontId],
      ['標準フォントサイズ', async () => (await chrome.fontSettings.getDefaultFontSize({})).pixelSize + ' px'],
      ['最小フォントサイズ', async () => (await chrome.fontSettings.getMinimumFontSize({})).pixelSize + ' px']
    ]
  },
  {
    id: 'features', icon: '⚙️', title: '対応機能（Web API）', desc: 'このChromeが使えるWeb機能の一覧です。',
    items: [
      ['セキュアコンテキスト', () => yn(isSecureContext)],
      ['クロスオリジン分離', () => yn(crossOriginIsolated)],
      ...[
        ['Service Worker', () => 'serviceWorker' in navigator], ['WebGL2', () => !!document.createElement('canvas').getContext('webgl2')],
        ['WebGPU', () => 'gpu' in navigator], ['WebAssembly', () => typeof WebAssembly === 'object'],
        ['WebRTC', () => 'RTCPeerConnection' in window], ['WebUSB', () => 'usb' in navigator],
        ['Web Bluetooth', () => 'bluetooth' in navigator], ['WebHID', () => 'hid' in navigator],
        ['Web Serial', () => 'serial' in navigator], ['WebXR', () => 'xr' in navigator],
        ['Web Share', () => 'share' in navigator], ['Web MIDI', () => 'requestMIDIAccess' in navigator],
        ['通知', () => 'Notification' in window], ['クリップボード', () => 'clipboard' in navigator],
        ['位置情報', () => 'geolocation' in navigator], ['音声認識', () => 'webkitSpeechRecognition' in window],
        ['IndexedDB', () => 'indexedDB' in window], ['Cache Storage', () => 'caches' in window],
        ['SharedArrayBuffer', () => typeof SharedArrayBuffer !== 'undefined'], ['OffscreenCanvas', () => 'OffscreenCanvas' in window],
        ['WebCodecs', () => 'VideoEncoder' in window], ['File System Access', () => 'showOpenFilePicker' in window],
        ['画面共有', () => !!navigator.mediaDevices?.getDisplayMedia], ['WebTransport', () => 'WebTransport' in window],
        ['EyeDropper', () => 'EyeDropper' in window], ['Compression Streams', () => 'CompressionStream' in window]
      ].map(([n, f]) => [n, () => ok(f())])
    ]
  },
  {
    id: 'plugins', icon: '📎', title: 'プラグイン・MIME', desc: 'ブラウザが内蔵するプラグインと対応ファイル形式。',
    items: [
      ['プラグイン', () => Array.from(navigator.plugins).map(p => p.name)],
      ['MIMEタイプ数', () => navigator.mimeTypes.length]
    ]
  },
  {
    id: 'more', icon: '🔗', title: 'さらに詳しく（chrome://ページ）', desc: 'Chrome内部の詳細ページを開きます。',
    links: [
      ['chrome://version', 'バージョン・実行パス・プロフィールパス'], ['chrome://gpu', 'GPUの詳細・ハードウェアアクセラレーション'],
      ['chrome://system', 'システム診断情報'], ['chrome://flags', '実験的機能'], ['chrome://policy', '適用中のポリシー'],
      ['chrome://components', 'コンポーネントの更新状況'], ['chrome://net-internals', 'ネットワーク内部情報'],
      ['chrome://memory-internals', 'メモリ内部情報'], ['chrome://process-internals', 'プロセス情報'],
      ['chrome://sync-internals', '同期の内部情報'], ['chrome://histograms', '内部統計'], ['chrome://inspect', 'デバッグ対象一覧'],
      ['chrome://extensions', '拡張機能の管理'], ['chrome://settings', '設定'], ['chrome://device-log', 'デバイスログ'],
      ['chrome://about', 'chrome:// 一覧']
    ]
  }
];

/* ========== 描画 ========== */
const STATE = {};                                   // セクションID -> { sec, rows, tables, busy, again }
const BYID = Object.fromEntries(SECTIONS.map(s => [s.id, s]));
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
const copy = (text, node) => {
  navigator.clipboard.writeText(text).then(() => {
    const old = node.style.outline; node.style.outline = '2px solid #16a34a';
    setTimeout(() => (node.style.outline = old), 500);
  });
};
const flash = n => { n.classList.remove('flash'); void n.offsetWidth; n.classList.add('flash'); };

function plain(v) {
  if (Array.isArray(v)) return v.join(', ');
  if (v && typeof v === 'object') return v.btn ? '' : JSON.stringify(v);
  return String(v);
}

function renderValue(node, v) {
  if (v && typeof v === 'object' && !Array.isArray(v) && v.btn) {
    const b = el('button', '', v.btn);
    b.onclick = async () => {
      b.disabled = true; b.textContent = '取得中…';
      try { node.textContent = await v.run(); } catch (e) { node.textContent = NA; node.classList.add('na'); }
    };
    node.append(b); return;
  }
  if (Array.isArray(v)) {
    if (!v.length) { node.textContent = 'なし'; node.classList.add('na'); return; }
    v.forEach(x => node.append(el('span', 'chip', plain(x))));
    node.dataset.copy = v.join('\n'); return;
  }
  const s = plain(v);
  node.textContent = s;
  if (s === NA) node.classList.add('na');
  else if (s.startsWith('✅')) node.classList.add('ok');
  else if (s.startsWith('❌') || s.startsWith('⛔')) node.classList.add('ng');
}

function setValue(node, v) {
  node.textContent = ''; node.className = 'v'; delete node.dataset.copy;
  renderValue(node, v);
}

async function resolveVal(def) {
  let val = def[1];
  if (typeof val === 'function') {
    try { val = await val(); } catch (e) { val = NA; }
    if (val === undefined || val === null || val === '') val = NA;
  }
  return val;
}
async function resolveTable(t) { try { return await t.rows(); } catch (e) { return null; } }

function makeTable(def) {
  const wrap = el('div', 'tbl'); const tb = el('table'); const cap = el('caption'); tb.append(cap);
  const thead = el('thead'), tr = el('tr'); def.cols.forEach(c => tr.append(el('th', '', c))); thead.append(tr); tb.append(thead);
  const tbody = el('tbody'); tb.append(tbody); wrap.append(tb);
  return { def, wrap, cap, tbody, sig: null, data: null };
}
function applyTable(T, data) {
  if (!data) { T.wrap.hidden = true; return; }
  T.wrap.hidden = false; T.data = data; T.sig = JSON.stringify(data);
  T.cap.textContent = `${T.def.title}（${data.length}件）`;
  T.tbody.textContent = '';
  if (!data.length) { const r = el('tr'); const td = el('td', '', 'データなし'); td.colSpan = T.def.cols.length; r.append(td); T.tbody.append(r); }
  data.forEach(r => { const rr = el('tr'); r.forEach(c => rr.append(el('td', '', String(c ?? '—')))); T.tbody.append(rr); });
}

async function buildSection(sec, card) {
  const body = $('.body', card);
  const st = { sec, rows: [], tables: [], busy: false, again: false };
  const vals = await Promise.all((sec.items || []).map(resolveVal));
  const datas = await Promise.all((sec.tables || []).map(resolveTable));
  body.textContent = '';
  (sec.items || []).forEach((def, i) => {
    const [k, , h, live] = def;
    const row = el('div', 'row'); const kk = el('div', 'k', k);
    if (h) kk.append(el('small', '', h));
    const v = el('div', 'v'); renderValue(v, vals[i]);
    if (!(vals[i] && vals[i].btn)) v.onclick = () => { if (!v.classList.contains('na')) copy(v.dataset.copy || v.textContent, v); };
    row.append(kk, v); body.append(row);
    st.rows.push({ def, k, vnode: v, sig: plain(vals[i]), live });
  });
  (sec.tables || []).forEach((t, i) => { const T = makeTable(t); applyTable(T, datas[i]); body.append(T.wrap); st.tables.push(T); });
  if (sec.links) {
    const g = el('div', 'links');
    sec.links.forEach(([url, d]) => {
      const b = el('button', 'link', url); b.append(el('small', '', d));
      b.onclick = () => chrome.tabs.create({ url }); g.append(b);
    });
    body.append(g);
  }
  STATE[sec.id] = st;
}

/* ========== 更新処理 ========== */
async function updateRow(r) {
  if (typeof r.def[1] !== 'function') return;
  const nv = await resolveVal(r.def); const s = plain(nv);
  if (s === r.sig) return;
  r.sig = s; setValue(r.vnode, nv); flash(r.vnode);
}
async function updateTable(T) {
  const d = await resolveTable(T.def);
  if (!d || JSON.stringify(d) === T.sig) return;
  applyTable(T, d);
}
async function refreshSection(id) {
  const st = STATE[id]; if (!st) return;
  if (st.busy) { st.again = true; return; }
  st.busy = true;
  try { await Promise.all([...st.rows.map(updateRow), ...st.tables.map(updateTable)]); }
  finally { st.busy = false; }
  if (st.again) { st.again = false; refreshSection(id); return; }
  updateSummary();
  if ($('#q').value) applyFilter();
}

/* ========== リアルタイム更新（このページが表示されている間だけ） ========== */
const visible = () => document.visibilityState === 'visible';
const dirty = new Set();       // 非表示中に変化があったセクション
const pending = {};
let tick = 0, ticker = null;

// イベント起点の更新（連続発火は間引く）
function trigger(id) {
  if (!visible()) { dirty.add(id); return; }
  if (pending[id]) return;
  pending[id] = setTimeout(() => {
    pending[id] = 0;
    if (visible()) refreshSection(id); else dirty.add(id);
  }, BYID[id].debounce ?? 400);
}

// 1秒ごとの定期更新（liveミリ秒を指定した項目 / pollを指定したセクション）
function onTick() {
  if (!visible()) return stopLive();
  tick++;
  for (const [id, st] of Object.entries(STATE)) {
    const poll = st.sec.poll;
    if (poll && tick % Math.round(poll / 1000) === 0) { refreshSection(id); continue; }
    const due = st.rows.filter(r => r.live && tick % Math.round(r.live / 1000) === 0);
    if (due.length) Promise.all(due.map(updateRow)).then(updateSummary);
  }
}
function startLive() { if (!ticker && visible()) ticker = setInterval(onTick, 1000); }
function stopLive() { if (ticker) clearInterval(ticker); ticker = null; }

function syncLive() {
  const live = $('#live');
  if (visible()) {
    startLive();
    // 非表示の間に見逃した分を、表示に戻った瞬間にまとめて反映
    dirty.forEach(id => refreshSection(id)); dirty.clear();
    Object.entries(STATE).forEach(([id, st]) => {
      if (st.sec.poll) refreshSection(id);
      else st.rows.filter(r => r.live).forEach(updateRow);
    });
    live.textContent = '🟢 リアルタイム更新中'; live.className = 'live on';
  } else {
    stopLive();
    live.textContent = '⏸ 一時停止中'; live.className = 'live';
  }
}
document.addEventListener('visibilitychange', syncLive);

let eventsBound = false;
function bindEvents() {
  if (eventsBound) return; eventsBound = true;
  SECTIONS.forEach(sec => {
    if (!sec.events) return;
    try { sec.events(() => trigger(sec.id)); } catch (e) { /* 未対応のイベントは無視 */ }
  });
}

/* ========== 要約・検索・出力 ========== */
const SUMMARY = [['🌐 Chrome', 'Chromeバージョン'], ['🖥️ OS', 'OS'], ['⚡ CPUコア', '論理CPUコア数'],
  ['📊 CPU使用率', 'CPU使用率（全体）'], ['🧠 メモリ', 'メモリ（正確）'], ['📐 画面', '画面解像度'], ['🈂️ 言語', '言語（優先）']];
const sumNodes = [];
function findValue(label) {
  for (const st of Object.values(STATE)) for (const r of st.rows) if (r.k === label) return r.sig;
  return '…';
}
function updateSummary() {
  sumNodes.forEach(([n, label]) => { const v = findValue(label); if (n.textContent !== v) n.textContent = v; });
}

function applyFilter() {
  const q = $('#q').value.trim().toLowerCase();
  document.querySelectorAll('.row, tbody tr, .link').forEach(r => { r.hidden = !!q && !r.textContent.toLowerCase().includes(q); });
  document.querySelectorAll('section.card').forEach(s => {
    const all = s.querySelectorAll('.row, tbody tr, .link');
    s.hidden = !!q && all.length > 0 && ![...all].some(r => !r.hidden);
  });
}

async function load() {
  const nav = $('#nav'), content = $('#content'), box = $('#summary');
  nav.textContent = ''; content.textContent = ''; box.textContent = ''; sumNodes.length = 0;
  Object.keys(STATE).forEach(k => delete STATE[k]);
  SUMMARY.forEach(([t, label]) => {
    const c = el('div', 'sum'); const v = el('div', 'v', '…'); c.append(el('div', 'l', t), v); box.append(c); sumNodes.push([v, label]);
  });
  const jobs = SECTIONS.map(sec => {
    const a = el('a', '', `${sec.icon} ${sec.title.replace(/（.*）/, '')}`); a.href = '#' + sec.id; nav.append(a);
    const card = el('section', 'card'); card.id = sec.id;
    card.append(el('h2', '', `${sec.icon} ${sec.title}`), el('p', 'desc', sec.desc));
    const body = el('div', 'body'); body.append(el('p', 'loading', '読み込み中…')); card.append(body);
    content.append(card);
    return buildSection(sec, card);
  });
  await Promise.all(jobs);
  updateSummary(); applyFilter(); bindEvents(); syncLive();
}

$('#q').addEventListener('input', applyFilter);
$('#reload').addEventListener('click', load);
$('#export').addEventListener('click', () => {
  const data = {};
  for (const st of Object.values(STATE)) {
    data[st.sec.title] = {
      items: Object.fromEntries(st.rows.map(r => [r.k, r.sig || r.vnode.textContent])),
      ...Object.fromEntries(st.tables.filter(t => t.data).map(t => [t.def.title, t.data]))
    };
  }
  const blob = new Blob([JSON.stringify({ exportedAt: new Date().toISOString(), data }, null, 2)], { type: 'application/json' });
  const a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'chrome-info.json'; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

load();
