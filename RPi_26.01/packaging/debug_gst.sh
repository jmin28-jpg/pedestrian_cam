#!/usr/bin/env bash
set -e
mkdir -p logs
rm -f logs/gst_debug_gstreamer.txt
export GST_DEBUG="GST_PLUGIN_LOADING:6,GST_REGISTRY:6,*:2"
export GST_DEBUG_NO_COLOR=1
export GST_DEBUG_FILE="logs/gst_debug_gstreamer.txt"

echo "---- gst tools check ----"
command -v gst-inspect-1.0 || true
gst-inspect-1.0 --version || true

echo "---- gst-launch probe (should create GST_DEBUG_FILE) ----"
gst-launch-1.0 -q fakesrc num-buffers=1 ! fakesink || true

./dist/OPAS-200 2>&1 | tee logs/gst_debug_runtime.txt
echo "---- gst debug file ----"
ls -lh logs/gst_debug_gstreamer.txt || true
echo "---- gst debug tail ----"
tail -n 200 logs/gst_debug_gstreamer.txt || true