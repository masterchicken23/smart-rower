import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from collections import deque
import datetime
import sys

# --- Configuration ---
SERIAL_PORT = 'COM6'  # UPDATE THIS to your RP2040's port
BAUD_RATE = 115200
WINDOW_SIZE = 100     # Number of data points to show on screen

# --- Initialize Serial Connection ---
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print(f"Connected to {SERIAL_PORT}")
except Exception as e:
    print(f"Error opening serial port: {e}")
    sys.exit()

# --- Initialize CSV File ---
# Generate filename based on current date and time
current_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
csv_filename = f"recordings/imu_data_{current_time}.csv"
csv_file = open(csv_filename, 'w')

# Write the header to the CSV file manually so it's always at the top, 
# even if we miss the Arduino's initial print over serial.
csv_header = "timestamp_us,a1x,a1y,a1z,g1x,g1y,g1z,a2x,a2y,a2z,g2x,g2y,g2z,a3x,a3y,a3z,g3x,g3y,g3z"
csv_file.write(csv_header + '\n')
print(f"Logging data to: {csv_filename}")

# --- Data Buffers for Plotting ---
t_data = deque(maxlen=WINDOW_SIZE)
imu_data = [deque(maxlen=WINDOW_SIZE) for _ in range(18)]

labels = ['X', 'Y', 'Z']
colors = ['r', 'g', 'b']

# --- Plot Setup ---
fig, axes = plt.subplots(3, 2, figsize=(14, 8), sharex=True)
fig.canvas.manager.set_window_title(f"Real-Time IMU Data (Logging to {csv_filename})")

lines = []

for i in range(3): # Rows: IMU 1, 2, 3
    for j in range(2): # Columns: Accel, Gyro
        ax = axes[i, j]
        ax.set_ylim(-32768, 32768) 
        
        sensor_type = 'Accelerometer' if j == 0 else 'Gyroscope'
        ax.set_title(f"IMU {i+1} {sensor_type}")
        ax.grid(True, linestyle='--', alpha=0.6)
        
        for k in range(3):
            line, = ax.plot([], [], label=labels[k], color=colors[k], linewidth=1.5)
            lines.append(line)
            
        ax.legend(loc='upper right')

plt.tight_layout()

# --- Animation Update Function ---
def update(frame):
    updated = False
    
    # Read all available data in the serial buffer
    while ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
        except UnicodeDecodeError:
            continue
            
        if not line:
            continue
            
        parts = line.split(',')
        
        # Check if we have the correct number of columns
        if len(parts) == 19:
            try:
                # 1. Parse data for plotting
                t = float(parts[0]) / 1000000.0
                vals = [float(x) for x in parts[1:]]
                
                t_data.append(t)
                for i in range(18):
                    imu_data[i].append(vals[i])
                updated = True
                
                # 2. Save raw line directly to CSV
                csv_file.write(line + '\n')
                
            except ValueError:
                # Ignores header string if sent by Arduino mid-stream
                pass

    if updated and len(t_data) > 0:
        current_t = t_data[-1]
        start_t = t_data[0]
        
        for ax in axes.flat:
            ax.set_xlim(start_t, current_t + 0.05) 
            
        for i in range(18):
            lines[i].set_data(t_data, imu_data[i])

    return lines

# --- Main Execution ---
try:
    ani = animation.FuncAnimation(fig, update, interval=1, blit=False, save_count=100)
    plt.show() # This blocks execution until you close the plot window
except KeyboardInterrupt:
    print("\nPlotting interrupted by user.")
finally:
    # Safely close serial port and file to prevent data corruption
    ser.close()
    csv_file.close()
    print(f"Serial port closed. File successfully saved to: {csv_filename}")
