# 入退室管理 キオスク端末
#
#   make dev API=http://localhost:8080   ウィンドウ表示で起動 (開発用、CAMERA=1 などでカメラ指定)
#   make run                             全画面で起動 (kiosk.ini の設定を使用)
#   make setup                           Pi に必要なパッケージ (カメラ含む) を入れる
#   make autostart                       ログイン時に自動起動する

API ?=
CAMERA ?=
SIZE ?= 1024x600

# macOS 標準の Python は Tk 8.5 で描画が壊れるため、uv があればそちらの Python を使う
# (カメラ読み取り用のライブラリも一緒に入れる)
ifeq ($(shell uname),Darwin)
  ifneq ($(shell command -v uv),)
    PYTHON ?= uv run --no-project --python 3.12 --with opencv-python --with zxing-cpp --with pillow python
  endif
endif
PYTHON ?= python3

APP := $(CURDIR)/dakoku_kiosk.py
API_ARG := $(if $(API),--api $(API)) $(if $(CAMERA),--camera $(CAMERA))
AUTOSTART := $(HOME)/.config/autostart/dakoku-kiosk.desktop

.PHONY: help run dev config setup autostart remove-autostart

help:
	@sed -n '3,6p' Makefile | sed 's/^# *//'

run: config
	$(PYTHON) $(APP) $(API_ARG)

dev:
	$(PYTHON) $(APP) --windowed --size $(SIZE) $(API_ARG)

config: kiosk.ini

kiosk.ini:
	cp kiosk.ini.example kiosk.ini
	@echo "kiosk.ini を作成しました。api_base_url を設定してください。"

# picamera2: Pi カメラモジュール / opencv: USB カメラ / pyzbar: デコード / pil: プレビュー
setup:
	sudo apt install -y python3-tk fonts-noto-cjk \
		python3-picamera2 python3-opencv python3-pyzbar libzbar0 python3-pil python3-pil.imagetk

autostart: config
	mkdir -p $(dir $(AUTOSTART))
	printf '%s\n' '[Desktop Entry]' 'Type=Application' 'Name=Dakoku Kiosk' \
		'Exec=/usr/bin/python3 $(APP)' 'X-GNOME-Autostart-enabled=true' > $(AUTOSTART)
	@echo "$(AUTOSTART) を作成しました。"

remove-autostart:
	rm -f $(AUTOSTART)
