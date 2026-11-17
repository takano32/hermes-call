# Hermes Call

Ubuntu Server に常駐し、**それ自体が SIP 通話クライアントとして動く** Hermes Agent 用ブリッジです。PJSIP で既存 SIP サーバーへ内線アカウントを REGISTER し、登録更新・発信・着信応答・RTP 音声を処理します。サーバーに音声デバイスは不要です。

Hermes のネイティブプラグインは、常駐ブリッジのローカル API を呼び出します。予約・繰り返しは **Hermes 標準 cron** に任せ、ブリッジは即時発信と通話を担当します。

```text
内線電話 ← SIP/RTP → 既存 SIP サーバー / PBX
                              ↕ REGISTER・INVITE・RTP
                   hermes-call（PJSIP 常駐クライアント）
                       ↕                   ↕
                Groq Whisper STT     公開 VOICEVOX API
                       ↘                   ↗
                          Hermes Agent
                              ↑
Hermes plugin / cron → localhost:8765 発信 API
```

- 音声認識: [Groq Whisper Large V3 Turbo](https://console.groq.com/docs/speech-to-text)。API キーが必要です。[Free Plan](https://console.groq.com/docs/rate-limits) には利用上限があります。有料認識 API への自動切替はありません。
- 音声合成: 指定された **[VOICEVOX Zundamon Public API](https://voicevox-api.swiftlybot.com/)**。ローカル ENGINE・Docker は不要です。`/v1/audio_query` に `{text,speaker}`、`/v1/synthesis` に `{speaker,audioQuery}` を JSON で送信します。
- 会話: インストール済み Hermes の認証・モデル設定を使います。モデル利用料は別です。

一度に 1 通話、発話後に約 2 秒の無音でターンを区切る方式です。再生中の割り込みと DTMF による操作は未対応です。1 発話は最大 30 秒、通話は標準で最大 30 分です。音声は 16kHz PCM で処理し、電話側との G.711 変換は PJSIP が行います。

## インストール

対象は Ubuntu Server 24.04 / Python 3.12 です。同じ Linux ユーザーで Hermes の `hermes setup` を完了させてください。

```bash
git clone https://github.com/techfish-11/hermes-call.git
cd hermes-call
bash scripts/install-ubuntu.sh
```

インストーラーは PATH と標準のインストール先から Hermes の実行ファイルを検出します。独自の場所に導入した場合は、実行パスを明示できます。

```bash
HERMES_BIN=/opt/hermes/.hermes/bin/hermes bash scripts/install-ubuntu.sh
```

検出だけを確認するには `bash scripts/find-hermes.sh` を実行します。普段のシェルで `hermes` が動くのに見つからない場合は、`type -a hermes` で alias・関数・実行ファイルの場所を確認してください。常駐サービスではシェルの alias・関数ではなく、インストール済み launcher の絶対パスを使用します。

スクリプトは必要なビルドツール、ffmpeg、専用 Python venv、PJSIP 2.17 の公式 Python バインディング、Hermes プラグイン、systemd サービスを配置します。PJSIP の初回コンパイルには時間がかかります。ローカル PBX はインストールしません。

初回はランダムな API トークンを生成します。既存の設定ファイルは上書きせず、サービスも設定前には起動しません。

`~/.config/hermes-call/config.json` を編集してください。[設定例](config.example.json) の主な項目:

| 項目 | 設定内容 |
|---|---|
| `sip_server` | REGISTER と SIP プロキシのホスト名または IPv4。ポートは含めない |
| `sip_domain` | SIP アカウントのドメイン。着信の From URI ドメインと一致させる |
| `sip_username` / `sip_password` | Hermes 用の SIP 内線アカウント |
| `sip_peer_ips` | 着信 INVITE を許可する SIP サーバーの IPv4 リスト |
| `allowed_extensions` | 発信先として許可する内線 |
| `allowed_callers` | 着信を許可する相手の内線 |
| `stt_api_key` | Groq API キー |
| `tts_url` | 標準で `https://voicevox-api.swiftlybot.com/v1` |
| `tts_speaker` | 標準で 3。公開 API が許可するずんだもんのスタイル ID |
| `hermes_command` | Hermes 実行ファイルの絶対パスを含む argv リスト |
| `hermes_toolsets` | 標準は `hermes_call,cronjob`。通話で必要なら `web` などを追加 |

ファイルはサービスユーザー所有・`chmod 600` にします。`HERMES_HOME` を使うプロファイルでは、プラグインの有効化先と systemd の環境を合わせてください。設定の場所は `HERMES_CALL_CONFIG` で変更できます。

```bash
hermes plugins enable hermes-call
hermes plugins doctor ~/.hermes/plugins/hermes-call --ci
~/.local/share/hermes-call/venv/bin/hermes-call --check
sudo systemctl enable --now hermes-call
sudo journalctl -u hermes-call -f
```

`--check` は設定と PJSUA2 の import を確認します。実際の SIP 登録はサービス起動後、ログの `SIP registration status=200 active=True` で確認してください。認証付き `/health` は登録済みで 200、未登録で 503 を返します。登録更新と通信断後の再登録は PJSIP が行います。

gateway を利用する場合はプラグイン有効化後に再起動し、cron も稼働させてください。プラグインだけを有効にしても SIP 待受は開始されません。常駐サービスが発着信を担当します。

## SIP サーバーとネットワーク

SIP サーバー側で Hermes 用内線（例: 7000）を作成します。内線電話から 7000 に電話すると、登録済みブリッジが着信へ応答します。PBX ごとのトランク設定は不要で、通常の SIP 電話と同じ登録方式です。アカウントに必要な内線への発信権限を設定してください。

標準のポートは SIP 5068/UDP と RTP/RTCP 40000–40021/UDP です。ホスト・ネットワークの firewall は必要な SIP/RTP サーバーとの通信に限定してください。SIP と RTP の接続元が異なる PBX では、それぞれのアドレスを許可します。NAT 越しで必要なら `stun_servers` を設定し、PBX 側の RTP 中継と戻り経路も確認してください。IPv4 を対象としています。

インターネット越しには `sip_transport: "tls"`、適切な `sip_port`、必要に応じて `sip_srtp_required: true` または VPN を使います。TLS のサーバー証明書は OS の CA ストアで検証し、検証の無効化はしません。

着信は **接続元 IP・SIP URI ドメイン・内線番号** の全条件で許可します。PBX が外線や不正な Caller ID を許可内線に見せないよう設定してください。ローカル発信 API の 8765 は `127.0.0.1` にのみ bind し、トークン認証を要求します。

## Hermes から使う

「内線1001に電話して、作業が終わったことを伝えて」と依頼すると `hermes_call_dial` が即時発信します。

```json
{
  "extension": "1001",
  "message": "作業が終わりました。確認したいことはありますか？",
  "request_id": "e17b1f961af54e22bd168cf230e7737c"
}
```

`request_id` は発信ごとに新しい 32 桁の小文字 hex にし、同じ発信のリトライでは同じ ID を使います。同じ ID は再発信しません。`dialing` は発信要求の提出を表し、回答・伝達完了を意味しません。`hermes_call_status` で直近 100 件の発信状態を確認できます。

「2時間後に内線1001に電話して」は Hermes の `cronjob_manage` に登録し、その時刻に `hermes_call_dial` を実行させます。通話中も同じように cron を利用できます。cron 実行側でも `hermes_call` toolset を有効にしてください。タスクには内線・伝言と実行専用の request_id を含めます。繰り返しタスクは実行ごとに別 ID、同一実行のリトライは同じ ID にします。

通話ごとに専用の名前付き Hermes セッションを使い、直近の通常チャットを勝手に再開しません。同じプロファイルの権限・メモリを使うため、電話相手は信頼できる内線に限定してください。

## 公開 VOICEVOX API とデータ

API の公開仕様は IP あたり既定で **60 秒に 30 リクエスト**です。合成には 2 リクエストを使うため、ブリッジ内でも各リクエストを最低 2.1 秒空けます。同じ IP の別アプリによる利用も上限に含まれます。429、API エラー、長時間の応答停止では通話を失敗として終了します。サービス側の仕様変更・利用制限は公開元を確認してください。

音声利用時のクレジットは **VOICEVOX:ずんだもん** です。冒頭で音声提供名を案内します。音声を公開する場合は公開元の表記条件と各ライブラリの規約にも従ってください。

発話音声は Groq へ、読み上げテキストは指定の VOICEVOX API へ送ります。一時 WAV は通話終了時に削除し、異常終了した残骸は次回起動時に削除します。発信履歴 SQLite は状態と冒頭の伝言を保持します。会話履歴は Hermes の保管設定に従います。ログには発話・パスワード・API のエラー本文を出しません。

プロセス終了時は通話を切断して SIP 登録を解除します。systemd が再起動・OS 起動時の常駐を担当します。サービス再起動時に成否不明の発信は `unknown` とし、自動再発信しません。通話中の追加発信は 409、未登録時の発信は 503 です。

以前の Asterisk 版の設定とは互換性がありません。旧 `ari_*`・`agi_port`・`endpoint_name` を削除し、新しい設定例へ移行してください。旧版を導入済みなら旧サービスを停止してからインストーラーを再実行し、不要になった Asterisk 設定は管理者が整理してください。

## 検証

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m ruff check .
bash -n scripts/install-ubuntu.sh scripts/build-pjsip.sh
```

通常の自動テストはネイティブ SIP と外部 HTTP をテストダブルで置き換え、入力検証、発信重複防止、登録状態、着信許可、PCM バッファ、API の JSON 契約を検証します。Linux のネイティブビルド・REGISTER・実通話・NAT・音声遅延・Hermes 本体との結合は別途実機で確認する必要があります。

SIP 実装は [PJSUA2](https://docs.pjsip.org/en/latest/pjsua2/intro.html) の公式バインディングを使用します。
