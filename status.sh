#!/bin/sh
set -eu
systemctl --user --no-pager status ai-monitor.service
gnome-extensions info ai-monitor@local
