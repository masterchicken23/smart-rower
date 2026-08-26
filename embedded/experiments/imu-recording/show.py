import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from collections import deque
import time

# --- Configuration ---
SERIAL_PORT = 'COM6'  # UPDATE THIS to your RP2040's port
BAUD_RATE = 115200
WINDOW_SIZE = 100     # Number of data points to show (100 pts @ 20Hz = 5 seconds)

# Initialize serial connection
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print(f"Connected to {SERIAL_PORT}")
except Exception as e:
    print(f"Error opening serial port: {e}")
    exit()

# --- Data Buffers ---
# 1 Time array + 18 Data arrays (3 IMUs * 6 axes)
t_data = deque(maxlen=WINDOW_SIZE)
imu_data = [deque(maxlen=WINDOW_SIZE) for _ in range(18)]

labels = ['X', 'Y', 'Z']
colors = ['r', 'g', 'b'] # Red=X, Green=Y, Blue=Z

# --- Plot Setup ---
fig, axes = plt.subplots(3, 2, figsize=(14, 8), sharex=True)
fig.canvas.manager.set_window_title("Real-Time IMU Data")

lines = []

# Build the 3x2 grid of subplots
for i in range(3): # Rows: IMU 1, 2, 3


    for j in range(2): # Columns: 0=Accel, 1=Gyro
        ax = axes[i, j]
        
        # Raw MPU6050 data is 16-bit signed int (-32768 to +32767). 
        # Fixed limits prevent matplotlib from lagging while trying to auto-scale.
        ax.set_ylim(-32768, 32768) 
        
        sensor_type = 'Accelerometer' if j == 0 else 'Gyroscope'
        ax.set_title(f"IMU {i+1} {sensor_type}")
        ax.grid(True, linestyle='--', alpha=0.6)
        
        # 3 axes per sensor (x, y, z)
        for k in range(3):
            line, = ax.plot([], [], label=labels[k], color=colors[k], linewidth=1.5)
            lines.append(line)
            
        ax.legend(loc='upper right')

plt.tight_layout()

# --- Animation Update Function ---
def update(frame):
    updated = False
    t0 = time.perf_counter()
    
    # Read ALL available lines in the serial buffer to prevent lag/backlog
    while ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
        except UnicodeDecodeError:
            continue # Ignore corrupted bytes
            
        if not line:
            continue
            
        parts = line.split(',')
        
        # Check if we have the correct number of columns (1 time + 18 data)
        if len(parts) == 19:
            try:
                # Convert timestamp from microseconds to seconds
                t = float(parts[0]) / 1000000.0
                vals = [float(x) for x in parts[1:]]
                
                t_data.append(t)
                for i in range(18):
                    # IMU 2 broken for now
                    if 5 < i < 12:
                        imu_data[i].append(0)
                        continue

                    imu_data[i].append(vals[i])
                updated = True
            except ValueError:
                # This catches and ignores the "timestamp_us,a1x..." CSV header
                pass

    t1 = time.perf_counter()

    print(f"{(t1 - t0) * 1000:.2f} ms ({t0:.2f} to {t0:.2f})")

    # Only redraw if new data was successfully processed
    if updated and len(t_data) > 0:
        current_t = t_data[-1]
        start_t = t_data[0]
        
        # Shift the X-axis to create the rolling/scrolling effect
        for ax in axes.flat:
            ax.set_xlim(start_t, current_t + 0.05) 
            
        # Update the data in all 18 line objects
        for i in range(18):
            lines[i].set_data(t_data, imu_data[i])

    return lines

# Interval=50ms gives standard 20fps GUI updates to match your 20Hz logging rate
ani = animation.FuncAnimation(fig, update, interval=1, blit=False, save_count=100)

plt.show()
ser.close()
