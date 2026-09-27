#!/usr/bin/env python3
"""入退室管理システム キオスク端末 (Tkinter 版)

index.html と同じ API を叩く軽量ネイティブ GUI。
カメラを常時動かし、バーコードをかざすだけで打刻する。
カメラが使えないときは「名前から選んで打刻」で代用できる。

必要なもの (Raspberry Pi OS の apt パッケージ名):
  - python3-tk                       : GUI
  - python3-picamera2 / python3-opencv : カメラ (Pi カメラ / USB カメラ)
  - python3-pyzbar (または zxing-cpp)  : バーコードのデコード
  - python3-pil.imagetk              : カメラ映像のプレビュー
USB バーコードリーダー (キーボード入力型) は追加ライブラリなしで使える。
"""

import argparse
import configparser
import json
import os
import queue
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from datetime import datetime

import camera

try:
    from PIL import ImageTk
except Exception:
    ImageTk = None

HERE = os.path.dirname(os.path.abspath(__file__))

# 配色 (tailwind の色に合わせる)
BG = "#f3f4f6"          # gray-100
CARD = "#ffffff"
TEXT = "#1f2937"        # gray-800
SUBTEXT = "#4b5563"     # gray-600
MUTED = "#6b7280"       # gray-500
BORDER = "#e5e7eb"      # gray-200
BLUE = "#3b82f6"
BLUE_DARK = "#2563eb"
GRAY_BTN = "#6b7280"
GRAY_BTN_DARK = "#4b5563"
GREEN = "#22c55e"
RED = "#ef4444"
PREVIEW_BG = "#111827"  # gray-900

REFRESH_INTERVAL_MS = 30_000      # ログ・在室者の定期更新
USERS_REFRESH_INTERVAL_MS = 300_000  # ユーザー一覧の定期更新
RESULT_AUTO_CLOSE_MS = 5_000      # 結果表示の自動クローズ
PICKER_AUTO_CLOSE_MS = 60_000     # 名前選択画面の自動クローズ
PREVIEW_INTERVAL_MS = 66          # カメラプレビューの更新間隔
SCANNER_RESET_SEC = 0.5           # これ以上キー入力が空いたらバーコード入力をリセット

SCAN_HINT = "バーコードをカメラにかざしてください"
PAUSED_HINT = "カメラは一時停止中です"


# ---------------------------------------------------------------- API

class Api:
    def __init__(self, base_url, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path, method="GET", body=None):
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            # エラー時もレスポンスの message を拾う
            try:
                payload = json.loads(e.read().decode("utf-8"))
                message = payload.get("message")
            except Exception:
                message = None
            raise ApiError(message or f"HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError("サーバーに接続できません") from e

    def users(self):
        return self._request("/users").get("users") or []

    def current_users(self):
        return self._request("/entrance/current").get("current_users") or []

    def access_logs(self, limit=10):
        return self._request(f"/entrance/logs?limit={limit}").get("access_logs") or []

    def entrance(self, barcode, access_type="auto"):
        return self._request("/entrance", method="POST",
                             body={"barcode": barcode, "type": access_type})


class ApiError(Exception):
    pass


def format_time(iso_time):
    try:
        dt = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return str(iso_time)


# ---------------------------------------------------------------- UI helpers

class FlatButton(tk.Label):
    """タッチでも押しやすいフラットなボタン (tk.Button は OS テーマに引きずられるため)"""

    def __init__(self, master, text, command, bg, active_bg, font, **kw):
        super().__init__(master, text=text, bg=bg, fg="white", font=font,
                         cursor="hand2", **kw)
        self._bg, self._active_bg, self._command = bg, active_bg, command
        self._enabled = True
        self.bind("<ButtonPress-1>", lambda e: self._enabled and self.config(bg=self._active_bg))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_release(self, event):
        if not self._enabled:
            return
        self.config(bg=self._bg)
        # ボタン外で指を離した場合は押下扱いにしない
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self._command()

    def set_enabled(self, enabled):
        self._enabled = enabled
        self.config(bg=self._bg if enabled else "#9ca3af")


def card(master, **kw):
    return tk.Frame(master, bg=CARD, highlightthickness=1,
                    highlightbackground=BORDER, **kw)




# ---------------------------------------------------------------- App

class KioskApp:
    def __init__(self, root, api, camera_options=None, fullscreen=True, hide_cursor=False, window_size=(1024, 600)):
        self.root = root
        self.api = api
        self.results = queue.Queue()   # ワーカースレッド -> UI スレッド
        self.users = []                # [{name, barcode}]
        self.busy = False
        self.scan_buffer = ""
        self.scan_last_key = 0.0
        self.overlay = None
        self.overlay_timer = None
        self.preview_photo = None
        self.idle_status = (SCAN_HINT, TEXT)
        self.camera_state = None

        root.title("入退室管理システム")
        root.configure(bg=BG)
        if fullscreen:
            root.attributes("-fullscreen", True)
            self.size = (root.winfo_screenwidth(), root.winfo_screenheight())
        else:
            root.geometry("%dx%d" % window_size)
            self.size = window_size
        if hide_cursor:
            root.config(cursor="none")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.bind("<Control-q>", lambda e: self.quit())
        root.bind("<Key>", self._on_key)

        self.scanner = None
        missing = camera.missing_requirements()
        if not missing:
            self.scanner = camera.Scanner(
                on_scan=lambda code: self.results.put(("scan", True, code)),
                on_status=lambda state, msg: self.results.put(("camera_status", True, (state, msg))),
                **(camera_options or {}))

        self._setup_fonts()
        self._build()

        if self.scanner:
            self.scanner.start()
            self._update_preview()
        else:
            self._set_preview_text("カメラ読み取りは使えません\n(未インストール: %s)" % ", ".join(missing))
            self._set_idle_status("「名前から選んで打刻」を使ってください", RED)

        self.root.after(100, self._poll_results)
        self._tick_clock()
        self.refresh_all()
        self.refresh_users()

    def quit(self):
        if self.scanner:
            self.scanner.stop()
            self.scanner.join(timeout=1)
        self.root.destroy()

    # ---- fonts / layout

    def _setup_fonts(self):
        families = set(tkfont.families(self.root))
        family = next((f for f in ("Noto Sans CJK JP", "Noto Sans JP",
                                   "IPAexGothic", "IPAGothic", "TakaoPGothic",
                                   "Hiragino Sans", "Yu Gothic UI", "Meiryo")
                       if f in families), "TkDefaultFont")
        # 画面サイズに合わせてフォントを拡縮 (1280x800 を基準)
        w, h = self.size
        scale = max(0.6, min(w / 1280, h / 800))

        def f(size, weight="normal"):
            return tkfont.Font(family=family, size=int(size * scale), weight=weight)

        self.f_date = f(16, "bold")
        self.f_time = f(30, "bold")
        self.f_title = f(16, "bold")
        self.f_body = f(14)
        self.f_small = f(12)
        self.f_button = f(18, "bold")
        self.f_list = f(16)
        self.pad = int(16 * scale)

    def _build(self):
        p = self.pad
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=p, pady=p)
        body.columnconfigure(0, weight=5, uniform="col")
        body.columnconfigure(1, weight=4, uniform="col")
        body.columnconfigure(2, weight=3, uniform="col")
        body.rowconfigure(0, weight=1)

        # --- 左: 時計 + カメラプレビュー + 名前選択ボタン
        left = card(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, p))

        self.lbl_date = tk.Label(left, bg=CARD, fg=SUBTEXT, font=self.f_date)
        self.lbl_date.pack(pady=(p // 2, 0))
        self.lbl_time = tk.Label(left, bg=CARD, fg=TEXT, font=self.f_time)
        self.lbl_time.pack()

        # 下から積んで、プレビューには残りの領域を全部使わせる
        buttons = tk.Frame(left, bg=CARD)
        buttons.pack(side="bottom", fill="x", padx=p, pady=(0, p))
        FlatButton(buttons, "名前から選んで打刻", self.open_picker, GRAY_BTN, GRAY_BTN_DARK,
                   self.f_body, pady=p // 2).pack(side="left", fill="x", expand=True)
        if self.scanner:
            self.btn_camera = FlatButton(buttons, "カメラ停止", self.toggle_camera, GRAY_BTN,
                                         GRAY_BTN_DARK, self.f_body, pady=p // 2, padx=p)
            self.btn_camera.pack(side="left", padx=(p // 2, 0))
        self.lbl_status = tk.Label(left, text=SCAN_HINT, bg=CARD, fg=TEXT,
                                   font=self.f_body, wraplength=1)
        self.lbl_status.pack(side="bottom", fill="x", padx=p, pady=p // 2)
        self.lbl_status.bind("<Configure>", lambda e: self.lbl_status.config(wraplength=e.width))

        self.preview = tk.Label(left, bg=PREVIEW_BG, fg="white", font=self.f_body,
                                text="カメラを起動中...", justify="center")
        self.preview.pack(fill="both", expand=True, padx=p)
        self.preview.bind("<Configure>", self._on_preview_resize)
        # 一時停止中は映像エリアのタップでも再開できる
        self.preview.bind("<ButtonRelease-1>", lambda e: self.scanner and self.resume_camera())

        # --- 中央: 入退室ログ
        mid = card(body)
        mid.grid(row=0, column=1, sticky="nsew", padx=(0, p))
        tk.Label(mid, text="入退室ログ (最新10件)", bg=CARD, fg=TEXT,
                 font=self.f_title, anchor="w").pack(fill="x", padx=p, pady=(p, 4))
        tk.Frame(mid, bg=BORDER, height=1).pack(fill="x", padx=p)
        self.log_frame = tk.Frame(mid, bg=CARD)
        self.log_frame.pack(fill="both", expand=True, padx=p, pady=(4, p))
        self._set_placeholder(self.log_frame, "ログを取得中...")

        # --- 右: 在室者
        right = card(body)
        right.grid(row=0, column=2, sticky="nsew")
        self.lbl_current_title = tk.Label(right, text="現在の在室者 (0人)", bg=CARD,
                                          fg=TEXT, font=self.f_title, anchor="w")
        self.lbl_current_title.pack(fill="x", padx=p, pady=(p, 4))
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x", padx=p)
        self.current_frame = tk.Frame(right, bg=CARD)
        self.current_frame.pack(fill="both", expand=True, padx=p, pady=(4, p))
        self._set_placeholder(self.current_frame, "在室者を取得中...")

    def _set_placeholder(self, frame, text, color=MUTED):
        for w in frame.winfo_children():
            w.destroy()
        tk.Label(frame, text=text, bg=CARD, fg=color, font=self.f_small,
                 anchor="w").pack(fill="x", pady=4)

    # ---- clock

    def _tick_clock(self):
        now = datetime.now()
        self.lbl_date.config(text=now.strftime("%Y年%m月%d日"))
        self.lbl_time.config(text=now.strftime("%H:%M:%S"))
        self.root.after(1000 - now.microsecond // 1000, self._tick_clock)

    # ---- camera preview

    def _on_preview_resize(self, event):
        if self.scanner:
            self.scanner.preview_size = (event.width, event.height)

    def _set_preview_text(self, text):
        self.preview_photo = None
        self.preview.config(image="", text=text)

    def _update_preview(self):
        img = self.scanner.take_preview()
        if img is not None and ImageTk is not None:
            self.preview_photo = ImageTk.PhotoImage(img)
            self.preview.config(image=self.preview_photo, text="")
        self.root.after(PREVIEW_INTERVAL_MS, self._update_preview)

    def _on_camera_status(self, ok, value):
        state, message = value
        previous, self.camera_state = self.camera_state, state
        if state == "starting":
            if previous in (None, "sleeping"):   # エラー後の再試行では表示を変えない
                self._set_preview_text("カメラを起動中...")
        elif state == "sleeping":
            self._set_preview_text(PAUSED_HINT + "\n\nタップすると再開します")
            self._set_idle_status(PAUSED_HINT, MUTED)
        elif state == "ready":
            if ImageTk is None:
                self._set_preview_text("カメラ読み取り中\n(プレビューには Pillow が必要です)")
            self._set_idle_status(SCAN_HINT, TEXT)
        elif state == "error":
            self._set_preview_text(f"カメラが使えません\n{message}\n\n自動で再接続します...")
            self._set_idle_status("「名前から選んで打刻」を使ってください", RED)

    def _set_idle_status(self, text, color):
        self.idle_status = (text, color)
        if not self.busy:
            self.lbl_status.config(text=text, fg=color)

    # ---- camera pause

    def toggle_camera(self):
        if self.scanner.awake.is_set():
            self.scanner.sleep()
            self.btn_camera.config(text="カメラ再開")
        else:
            self.resume_camera()

    def resume_camera(self):
        if not self.scanner.awake.is_set():
            self.scanner.wake()
            self.btn_camera.config(text="カメラ停止")

    # ---- background work

    def _run(self, kind, func, *args):
        def worker():
            try:
                self.results.put((kind, True, func(*args)))
            except Exception as e:  # noqa: BLE001 - UI に表示するため全部拾う
                self.results.put((kind, False, e))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_results(self):
        try:
            while True:
                kind, ok, value = self.results.get_nowait()
                getattr(self, f"_on_{kind}")(ok, value)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_results)

    def refresh_all(self, schedule=True):
        self._run("logs", self.api.access_logs, 10)
        self._run("current", self.api.current_users)
        if schedule:
            self.root.after(REFRESH_INTERVAL_MS, self.refresh_all)

    def refresh_users(self):
        self._run("users", self.api.users)
        self.root.after(USERS_REFRESH_INTERVAL_MS, self.refresh_users)

    # ---- result handlers

    def _on_users(self, ok, value):
        if ok:
            self.users = value

    def _on_logs(self, ok, value):
        frame = self.log_frame
        if not ok:
            self._set_placeholder(frame, "入退室ログの取得に失敗しました", RED)
            return
        if not value:
            self._set_placeholder(frame, "ログがありません")
            return
        for w in frame.winfo_children():
            w.destroy()
        for log in value[-10:]:
            is_entry = log.get("access_type") == "entry"
            row = tk.Frame(frame, bg=CARD)
            row.pack(fill="x", pady=2)
            # 時刻を先に pack して、幅が足りないときは名前の方を削る
            tk.Label(row, text=f"{'入室' if is_entry else '退室'} {format_time(log.get('time'))}",
                     bg=CARD, fg=MUTED, font=self.f_small).pack(side="right")
            tk.Label(row, text="●", bg=CARD, fg=GREEN if is_entry else RED,
                     font=self.f_small).pack(side="left")
            tk.Label(row, text=log.get("user_name", ""), bg=CARD, fg=TEXT,
                     font=self.f_body, anchor="w").pack(side="left", padx=(4, 0))
            tk.Frame(frame, bg="#f3f4f6", height=1).pack(fill="x")

    def _on_current(self, ok, value):
        frame = self.current_frame
        if not ok:
            self._set_placeholder(frame, "在室ユーザーの取得に失敗しました", RED)
            return
        self.lbl_current_title.config(text=f"現在の在室者 ({len(value)}人)")
        if not value:
            self._set_placeholder(frame, "在室者はいません")
            return
        for w in frame.winfo_children():
            w.destroy()
        for user in value:
            tk.Label(frame, text=user.get("user_name", ""), bg=CARD, fg=TEXT,
                     font=self.f_body, anchor="w").pack(fill="x", pady=2)
            tk.Frame(frame, bg="#f3f4f6", height=1).pack(fill="x")

    def _on_scan(self, ok, code):
        self.punch(code)

    def _on_entrance(self, ok, value):
        self._set_busy(False)
        if not ok:
            self.show_error(str(value))
            return
        self.refresh_all(schedule=False)
        self.show_result(value.get("entrance_log") or {})

    # ---- actions

    def _set_busy(self, busy):
        self.busy = busy
        if self.scanner:
            # 送信中はカメラの検出を通知させない (かざしっぱなしでも再送しない)
            (self.scanner.accepting.clear if busy else self.scanner.accepting.set)()
        text, color = ("送信中...", BLUE) if busy else self.idle_status
        self.lbl_status.config(text=text, fg=color)

    def punch(self, barcode):
        if self.busy:
            return
        self._set_busy(True)
        self._run("entrance", self.api.entrance, barcode, "auto")

    def _on_key(self, event):
        """USB バーコードリーダー (キーボード入力) の読み取り"""
        now = time.monotonic()
        if now - self.scan_last_key > SCANNER_RESET_SEC:
            self.scan_buffer = ""
        self.scan_last_key = now
        if event.keysym in ("Return", "KP_Enter"):
            code, self.scan_buffer = self.scan_buffer.strip(), ""
            if code:
                self.punch(code)
        elif event.char and event.char.isprintable():
            self.scan_buffer += event.char

    # ---- overlays

    def _open_overlay(self, header_text, header_color, lines=(), build=None,
                      auto_close_ms=RESULT_AUTO_CLOSE_MS):
        self.close_overlay()
        p = self.pad
        shade = tk.Frame(self.root, bg="#6b7280")
        shade.place(x=0, y=0, relwidth=1, relheight=1)
        shade.bind("<Button-1>", lambda e: self.close_overlay())
        box = tk.Frame(shade, bg=CARD)
        if build:
            box.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.6, relheight=0.9)
        else:
            box.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.5)
        tk.Label(box, text=header_text, bg=header_color, fg="white",
                 font=self.f_button, anchor="w", padx=p, pady=p).pack(fill="x")
        # フッターを先に下へ置き、本文は残りの領域を使う
        footer = tk.Frame(box, bg=CARD)
        footer.pack(side="bottom", fill="x", padx=p, pady=p)
        FlatButton(footer, "閉じる", self.close_overlay, GRAY_BTN, GRAY_BTN_DARK,
                   self.f_body, padx=p * 2, pady=p // 2).pack(side="right")
        tk.Frame(box, bg=BORDER, height=1).pack(side="bottom", fill="x")
        content = tk.Frame(box, bg=CARD)
        content.pack(fill="both", expand=True, padx=p * 1.5, pady=p)
        for text, font in lines:
            tk.Label(content, text=text, bg=CARD, fg=TEXT, font=font,
                     anchor="w", justify="left").pack(fill="x", pady=2)
        if build:
            build(content)
        self.overlay = shade
        self.overlay_timer = self.root.after(auto_close_ms, self.close_overlay)

    def close_overlay(self):
        if self.overlay_timer:
            self.root.after_cancel(self.overlay_timer)
            self.overlay_timer = None
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None

    def show_result(self, log):
        is_entry = log.get("access_type") == "entry"
        self._open_overlay(
            "入室しました" if is_entry else "退室しました",
            GREEN if is_entry else RED,
            [(log.get("user_name", ""), self.f_title),
             (f"時刻: {format_time(log.get('time'))}", self.f_body),
             (f"入場可能回数: {log.get('remaining_entries', '-')}", self.f_body),
             (f"総入場回数: {log.get('total_entries', '-')}", self.f_body)])

    def show_error(self, message):
        self._open_overlay("エラー", RED, [(message, self.f_body)])

    def open_picker(self):
        self._open_overlay("名前から選んで打刻", BLUE, build=self._build_picker,
                           auto_close_ms=PICKER_AUTO_CLOSE_MS)

    def _build_picker(self, content):
        p = self.pad
        if not self.users:
            tk.Label(content, text="ユーザー一覧を取得できていません", bg=CARD, fg=RED,
                     font=self.f_body).pack(pady=p)
            self.refresh_users()
            return
        users = list(self.users)

        def submit():
            sel = listbox.curselection()
            if sel:
                self.punch(users[sel[0]].get("barcode"))

        button = FlatButton(content, "入退室記録", submit, BLUE, BLUE_DARK,
                            self.f_button, pady=p // 2)
        button.pack(side="bottom", fill="x", pady=(p // 2, 0))
        button.set_enabled(False)

        list_frame = tk.Frame(content, bg=CARD)
        list_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame, width=int(p * 2))
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(
            list_frame, font=self.f_list, activestyle="none", exportselection=False,
            selectbackground=BLUE, selectforeground="white", bg=CARD, fg=TEXT,
            highlightthickness=1, highlightbackground=BORDER, relief="flat",
            yscrollcommand=scrollbar.set, height=1)  # 高さは残り領域に任せる
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)
        for user in users:
            listbox.insert("end", user.get("name", ""))
        listbox.bind("<<ListboxSelect>>",
                     lambda e: button.set_enabled(bool(listbox.curselection())))


# ---------------------------------------------------------------- main

def load_config(path):
    cfg = configparser.ConfigParser()
    cfg.read(path, encoding="utf-8")
    return cfg["kiosk"] if cfg.has_section("kiosk") else {}


def is_true(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser(description="入退室管理 キオスク端末")
    parser.add_argument("--config", default=os.path.join(HERE, "kiosk.ini"))
    parser.add_argument("--api", help="API のベース URL (conf.js の API_BASE_URL)")
    parser.add_argument("--camera", help="auto / picamera2 / デバイス番号 / デバイスパス")
    parser.add_argument("--windowed", action="store_true", help="全画面にしない (開発用)")
    parser.add_argument("--size", default="1024x600", help="--windowed 時のウィンドウサイズ")
    parser.add_argument("--hide-cursor", action="store_true", help="マウスカーソルを隠す")
    args = parser.parse_args()

    cfg = load_config(args.config)
    api_url = args.api or os.environ.get("DAKOKU_API_BASE_URL") or cfg.get("api_base_url")
    if not api_url:
        parser.error("API の URL を --api / DAKOKU_API_BASE_URL / kiosk.ini のいずれかで指定してください")

    fullscreen = not args.windowed and is_true(cfg.get("fullscreen", "true"))
    hide_cursor = args.hide_cursor or is_true(cfg.get("hide_cursor", "false"))
    camera_options = {
        "source": args.camera or cfg.get("camera", "auto"),
        "mirror": is_true(cfg.get("camera_mirror", "true")),
        "cooldown": float(cfg.get("scan_cooldown", 5)),
    }

    root = tk.Tk()
    KioskApp(root, Api(api_url, timeout=float(cfg.get("timeout", 10))),
             camera_options=camera_options, fullscreen=fullscreen, hide_cursor=hide_cursor,
             window_size=tuple(int(v) for v in args.size.split("x")))
    root.mainloop()


if __name__ == "__main__":
    main()
