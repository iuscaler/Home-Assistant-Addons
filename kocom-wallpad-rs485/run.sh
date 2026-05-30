#!/bin/sh

echo "[Info] Run Kocom Wallpad with RS485!"
python3 /kocom.py

# for dev
while true; do echo "still live"; sleep 100; done
