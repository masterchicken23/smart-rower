import serial
import csv
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from datetime import datetime

PORT = "/dev/cu.usbmodem2101"
BAUD = 115200
OUTFILE = f"distance_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

ser = serial.Serial(PORT, BAUD, timeout=1)
ser.readline()  # skip header

csvfile = open(OUTFILE, "w", newline="")
writer = csv.writer(csvfile)
writer.writerow(["timestamp_ms", "distance_mm", "phase"])

times = []
distances = []
prev_dist = None
phase = "unknown"
prev_phase = "unknown"

total_strokes = 0
stroke_times = []  # timestamps of each drive start
total_distance_mm = 0

fig, ax = plt.subplots()
line, = ax.plot([], [], lw=2)
phase_text = ax.text(0.02, 0.95, "", transform=ax.transAxes, fontsize=12,
                     verticalalignment='top', color='red')
stats_text = ax.text(0.02, 0.85, "", transform=ax.transAxes, fontsize=10,
                     verticalalignment='top', color='white')
ax.set_facecolor('black')
fig.patch.set_facecolor('black')
ax.tick_params(colors='white')
ax.xaxis.label.set_color('white')
ax.yaxis.label.set_color('white')
ax.title.set_color('white')
ax.set_xlabel("Time (ms)")
ax.set_ylabel("Distance (mm)")
ax.set_title("Live Distance Readout")
ax.set_ylim(0, 1100)

def update(frame):
    global prev_dist, phase, prev_phase, total_strokes, total_distance_mm

    raw = ser.readline().decode().strip()
    if "," in raw:
        try:
            t, d = raw.split(",")
            t, d = int(t), int(d)

            if prev_dist is not None:
                delta = d - prev_dist
                total_distance_mm += abs(delta)

                if delta > 5:
                    phase = "RECOVERY"
                elif delta < -5:
                    phase = "DRIVE"

                # count stroke on transition from recovery to drive
                if prev_phase == "RECOVERY" and phase == "DRIVE":
                    total_strokes += 1
                    stroke_times.append(t)

            prev_phase = phase
            prev_dist = d
            times.append(t)
            distances.append(d)
            writer.writerow([t, d, phase])
            csvfile.flush()

            # avg stroke rate: strokes per minute over last 10 strokes
            avg_spm = 0
            if len(stroke_times) >= 2:
                window = stroke_times[-10:]
                elapsed_min = (window[-1] - window[0]) / 60000.0
                if elapsed_min > 0:
                    avg_spm = (len(window) - 1) / elapsed_min

            print(f"{t} ms : {d} mm : {phase} : {total_strokes} strokes : {avg_spm:.1f} spm : {total_distance_mm/1000:.2f} m")

            ax.set_xlim(max(0, t - 10000), t + 500)
            line.set_data(times, distances)
            phase_text.set_text(f"Phase: {phase}")
            stats_text.set_text(
                f"Strokes: {total_strokes}\n"
                f"Avg rate: {avg_spm:.1f} spm\n"
                f"Distance: {total_distance_mm/1000:.2f} m"
            )
        except:
            pass
    return line, phase_text, stats_text,

ani = animation.FuncAnimation(fig, update, interval=50, cache_frame_data=False)

try:
    plt.show()
finally:
    csvfile.close()
    ser.close()