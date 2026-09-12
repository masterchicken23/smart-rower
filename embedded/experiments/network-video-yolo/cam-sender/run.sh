#!/bin/sh
python3 pi_sender.py --bind 'tcp://*:5555' --ratio 40 --fps 15 --hw-jpeg
