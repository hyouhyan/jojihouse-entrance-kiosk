# 入退室管理 キオスク端末 (Raspberry Pi 用)

`index.html` と同じ API を使う、Chromium 不要の軽量 GUI (Python + Tkinter)。
カメラを常時動かし、バーコードをかざすだけで打刻する。

## 使い方

```sh
make dev API=http://localhost:8080   # ウィンドウ表示で起動 (SIZE=800x480 / CAMERA=1 で指定可)
make run                             # 全画面で起動 (kiosk.ini の設定を使用)
```

API の URL は `--api` (`make` では `API=`) > 環境変数 `DAKOKU_API_BASE_URL` > `kiosk.ini` の `api_base_url` の順に優先。
`kiosk.ini` は初回の `make run` で `kiosk.ini.example` からコピーされる。

macOS では標準 Python の Tk 8.5 だと描画が壊れるため、`uv` があれば自動で uv の Python 3.12
(+ opencv / zxing-cpp / pillow) を使う (`PYTHON=` で上書き可)。
初回はターミナルアプリにカメラへのアクセス許可を求められる。

## Raspberry Pi へのセットアップ

```sh
make setup          # python3-tk, 日本語フォント, カメラ関連 (picamera2 / opencv / pyzbar / pillow)
make config         # kiosk.ini を作成 → api_base_url を編集
make autostart      # ログイン時に全画面で自動起動 (解除は make remove-autostart)
```

## 打刻の方法

1. **カメラ (メイン)**: バーコードをカメラにかざすと即打刻。結果は 5 秒で自動的に閉じる。
   - Pi カメラモジュール (picamera2) → USB カメラ (OpenCV) の順に自動で探す。`kiosk.ini` の `camera` で固定も可。
   - 同じバーコードは、カメラから外して `scan_cooldown` 秒 (既定 5 秒) 経つまで再打刻しない。
     かざしっぱなしで入室→退室と連続打刻されることはない。
   - カメラが外れた・見つからないときは画面に表示し、5 秒ごとに自動で再接続を試みる。
   - 「カメラを一時停止」ボタンでカメラを止められる (省エネ用)。「カメラを再開」ボタンか映像エリアのタップで再開。
2. **名前から選んで打刻**: カメラが使えないとき・カードを忘れたとき用。選んだ後に確認画面が出る。
3. **USB バーコードリーダー** (キーボード入力型): 追加設定なしで使える。

在室者・ログは 30 秒ごと、ユーザー一覧は 5 分ごとに自動更新。終了は `Ctrl+Q`。
