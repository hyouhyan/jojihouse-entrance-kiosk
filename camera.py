"""カメラ入力とバーコード読み取り

カメラ:   picamera2 (Pi カメラモジュール) → OpenCV (USB カメラ) の順に試す
デコーダ: pyzbar → zxing-cpp の順に試す
プレビュー表示には Pillow を使う (なければ読み取りのみ)
"""

import threading
import time

try:
    import cv2
except Exception:  # 未インストール以外に numpy 不整合などで壊れている場合もある
    cv2 = None

try:
    from picamera2 import Picamera2
except Exception:
    Picamera2 = None

try:
    from PIL import Image, ImageOps
except Exception:
    Image = None


def _make_decoder():
    try:
        from pyzbar import pyzbar
        return lambda gray: [s.data.decode("utf-8", errors="ignore") for s in pyzbar.decode(gray)]
    except Exception:
        pass
    try:
        import zxingcpp
        return lambda gray: [r.text for r in zxingcpp.read_barcodes(gray)]
    except Exception:
        return None


decode = _make_decoder()


def missing_requirements():
    """カメラ読み取りに足りないものを返す (空なら利用可能)"""
    missing = []
    if Picamera2 is None and cv2 is None:
        missing.append("picamera2 または opencv")
    if decode is None:
        missing.append("pyzbar または zxing-cpp")
    return missing


# ---------------------------------------------------------------- sources

class PiCamera:
    def __init__(self, size):
        self.cam = Picamera2()
        self.cam.configure(self.cam.create_preview_configuration(
            main={"size": size, "format": "RGB888"}))
        self.cam.start()

    def read(self):
        # picamera2 の "RGB888" は実際には BGR 順
        return self.cam.capture_array()[:, :, ::-1].copy()

    def close(self):
        self.cam.close()


class CvCamera:
    def __init__(self, source, size):
        self.cap = cv2.VideoCapture(source)
        if not self.cap.isOpened():
            raise RuntimeError("カメラを開けません")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # 古いフレームを溜めない

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("カメラから映像を取得できません")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def close(self):
        self.cap.release()


def open_camera(source="auto", size=(640, 480)):
    """source: "auto" / "picamera2" / デバイス番号 / デバイスパス・動画ファイル"""
    if source in ("auto", "picamera2") and Picamera2 is not None:
        try:
            return PiCamera(size)
        except Exception:
            if source == "picamera2":
                raise
    if source == "picamera2":
        raise RuntimeError("picamera2 がインストールされていません")
    if cv2 is None:
        raise RuntimeError("opencv がインストールされていません")
    if source == "auto":
        source = 0
    elif str(source).isdigit():
        source = int(source)
    return CvCamera(source, size)


# ---------------------------------------------------------------- scanner

class Scanner(threading.Thread):
    """カメラを常時読み取り、バーコードを検出したら on_scan(code) を呼ぶ。

    同じコードは、写らなくなってから cooldown 秒経つまで再通知しない
    (カードをかざしっぱなしで入室→退室と連続打刻されるのを防ぐ)。
    sleep() でカメラを閉じて待機し、wake() で再びカメラを開く (省エネ用)。
    コールバックはこのスレッドから呼ばれるので、受け側でスレッド間の受け渡しをすること。
    """

    FPS = 12
    RETRY_SEC = 5

    def __init__(self, on_scan, on_status, source="auto", cooldown=5.0, mirror=True):
        super().__init__(daemon=True)
        self.on_scan = on_scan
        self.on_status = on_status
        self.source = source
        self.cooldown = cooldown
        self.mirror = mirror
        self.accepting = threading.Event()   # クリア中は検出しても通知しない
        self.accepting.set()
        self.stopped = threading.Event()
        self.awake = threading.Event()       # クリア中はカメラを閉じて待機
        self.awake.set()
        self._kick = threading.Event()       # 待機中のスレッドを起こす
        self.preview_size = None             # UI 側から (w, h) を設定
        self._lock = threading.Lock()
        self._preview = None
        self._last_seen = {}

    def stop(self):
        self.stopped.set()
        self._kick.set()

    def sleep(self):
        self.awake.clear()
        self._kick.set()

    def wake(self):
        self.awake.set()
        self._kick.set()

    def take_preview(self):
        """最新のプレビュー画像 (PIL.Image) を取り出す。なければ None"""
        with self._lock:
            img, self._preview = self._preview, None
        return img

    def run(self):
        while not self.stopped.is_set():
            self._kick.clear()
            if not self.awake.is_set():
                self.on_status("sleeping", None)
                self._kick.wait()
                continue
            try:
                self.on_status("starting", None)
                cam = open_camera(self.source)
                try:
                    self.on_status("ready", None)
                    self._loop(cam)
                    continue
                finally:
                    cam.close()
            except Exception as e:
                self.on_status("error", str(e))
            # 失敗時は少し待って再試行 (sleep / wake / stop が来たらすぐ抜ける)
            self._kick.clear()
            self._kick.wait(self.RETRY_SEC)

    def _loop(self, cam):
        interval = 1 / self.FPS
        while not self.stopped.is_set() and self.awake.is_set():
            started = time.monotonic()
            rgb = cam.read()
            self._detect(rgb[:, :, 1].copy())   # 緑チャンネルをグレースケール代わりに使う
            self._make_preview(rgb)
            self.stopped.wait(max(0, interval - (time.monotonic() - started)))

    def _detect(self, gray):
        now = time.monotonic()
        for code in {c.strip() for c in decode(gray) if c.strip()}:
            last = self._last_seen.get(code, 0)
            self._last_seen[code] = now
            if now - last > self.cooldown and self.accepting.is_set():
                self.on_scan(code)
        # 古い記録を掃除
        self._last_seen = {c: t for c, t in self._last_seen.items() if now - t < 60}

    def _make_preview(self, rgb):
        if Image is None or not self.preview_size:
            return
        img = Image.fromarray(rgb)
        if self.mirror:
            img = ImageOps.mirror(img)
        img.thumbnail(self.preview_size)
        with self._lock:
            self._preview = img
