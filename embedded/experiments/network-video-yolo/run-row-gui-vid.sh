#!/bin/sh
export DISPLAY=:1001
python3 pose_stream.py --connect tcp://10.88.164.213:5555 --model yolo26n-pose.engine --fps 15 --rowing --max-age-ms 150 --gui --side left --smooth-hz 3 --max-det 1 --publish 'tcp://*:5556'
