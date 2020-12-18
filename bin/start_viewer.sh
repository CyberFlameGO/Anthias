#!/bin/bash

# Fixes permission on /dev/vchiq
chgrp video /dev/vchiq
chmod g+rwX /dev/vchiq

# Set permission for sha file
chown viewer /dev/snd/*
chown viewer /data/.screenly/latest_screenly_sha

# Fixes caching in QTWebEngine
mkdir -p /data/.local/share/ScreenlyWebview/QtWebEngine /data/.cache/ScreenlyWebview/
chown -R viewer /data/.local/share/ScreenlyWebview
chown -R viewer /data/.cache/ScreenlyWebview/

# SUGUSR1 from the viewer is also sent to the container
# Prevent it so that the container does not fail
trap '' 16

sudo -E -u viewer dbus-run-session python viewer.py &

# Waiting for the viewer
while true; do
  PID=$(pidof python)
  if [ "$?" == '0' ]; then
    break
  fi
  sleep 0.5
done

# Exit when the viewer falls
while kill -0 "$PID"; do
  sleep 1
done
