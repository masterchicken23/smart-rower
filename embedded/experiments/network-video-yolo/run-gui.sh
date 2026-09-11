#!/bin/sh
export DISPLAY=:1001
./pose_stream.py --connect tcp://cam-pi:5555 --model yolo26n-pose.engine --gui
