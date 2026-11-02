#!/bin/sh
set -eu
if [ "$(uname -s)" = Darwin ]; then
    launchctl print "gui/$(id -u)/io.github.remkobrenters.ai-monitor" | grep -E '^	(state|pid|last exit code) ='
    launchctl print "gui/$(id -u)/io.github.remkobrenters.ai-monitor.menu" | grep -E '^	(state|pid|last exit code) ='
    exit
fi
systemctl --user --no-pager status ai-monitor.service
gnome-extensions info ai-monitor@local
