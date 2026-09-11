#!/bin/sh
export DISPLAY=:1001
python3 pose_stream.py --connect tcp://cam-pi.local:5555 --model yolo26n-pose.engine --fps 15 --rowing --max-age-ms 150 --gui
