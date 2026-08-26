import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import math
import sys

# --- Configuration ---
SERIAL_PORT = 'COM6'  # UPDATE THIS
BAUD_RATE = 115200

# MPU6050 Scale Factors based on Arduino configuration
# Gyro +/- 1000 deg/s
GYRO_SCALE = 131.0
# Accel +/- 4g (Division isn't strictly necessary for atan2, but good for form)
ACCEL_SCALE = 8192.0 

# Complementary Filter Constant
# High alpha trusts the gyro more (smooth), low alpha trusts accel more (responsive but noisy)
ALPHA = 0.96 

# --- Filter States ---
# [IMU1, IMU2, IMU3]
pitch = [0.0, 0.0, 0.0]
roll = [0.0, 0.0, 0.0]
last_time = None

# --- Initialize Serial ---
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print(f"Connected to {SERIAL_PORT}")
except Exception as e:
    print(f"Error opening serial: {e}")
    sys.exit()

# --- Plot Setup ---
fig = plt.figure(figsize=(15, 5))
fig.canvas.manager.set_window_title("IMU Surface Levels")

axes = []
polys = []

# Base 3D square coordinates (a flat surface)
base_square = np.array([
    [-1, -1, 0],
    [ 1, -1, 0],
    [ 1,  1, 0],
    [-1,  1, 0]
])

for i in range(3):
    ax = fig.add_subplot(1, 3, i+1, projection='3d')
    ax.set_title(f"IMU {i+1} Level")
    
    # Set static limits to prevent zooming/scaling during rotation
    ax.set_xlim([-1.5, 1.5])
    ax.set_ylim([-1.5, 1.5])
    ax.set_zlim([-1.5, 1.5])
    ax.set_xlabel('X (Roll)')
    ax.set_ylabel('Y (Pitch)')
    ax.set_zlabel('Z')
    
    # Turn off the tick labels for a cleaner "instrument" look
    ax.set_xticklabels([])
    ax.set_yticklabels([])
    ax.set_zticklabels([])
    
    # Create the 3D polygon surface
    poly = Poly3DCollection([base_square], facecolors='cyan', edgecolors='blue', alpha=0.7)
    ax.add_collection3d(poly)
    
    # Add a stem/pillar in the middle to make tilt more obvious
    ax.plot([0, 0], [0, 0], [-1.5, 0], color='gray', linestyle='--', linewidth=2)
    
    axes.append(ax)
    polys.append(poly)

plt.tight_layout()

# --- 3D Rotation Math ---
def get_rotation_matrix(pitch_deg, roll_deg):
    # Convert to radians
    p = math.radians(pitch_deg)
    r = math.radians(roll_deg)
    
    # Roll matrix (X-axis rotation)
    Rx = np.array([
        [1, 0, 0],
        [0, math.cos(r), -math.sin(r)],
        [0, math.sin(r), math.cos(r)]
    ])
    
    # Pitch matrix (Y-axis rotation)
    Ry = np.array([
        [math.cos(p), 0, math.sin(p)],
        [0, 1, 0],
        [-math.sin(p), 0, math.cos(p)]
    ])
    
    # Combined rotation (assuming 0 yaw)
    return Ry @ Rx

# --- Update Function ---
def update(frame):
    global last_time, pitch, roll
    updated = False
    
    # Drain the serial buffer to maintain real-time performance
    while ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
        except UnicodeDecodeError:
            continue
            
        if not line:
            continue
            
        parts = line.split(',')
        if len(parts) == 19:
            try:
                current_time = float(parts[0]) / 1000000.0 # Convert us to seconds
                
                # Initialize timing on first read
                if last_time is None:
                    last_time = current_time
                    continue
                
                dt = current_time - last_time
                last_time = current_time
                
                # Prevent massive jumps if the serial hangs for a moment
                if dt > 0.5: 
                    dt = 0.05 

                # Process each of the 3 IMUs
                for i in range(3):
                    idx = 1 + (i * 6) # Base index for this IMU's data
                    
                    # Read Accel
                    ax = float(parts[idx])
                    ay = float(parts[idx+1])
                    az = float(parts[idx+2])
                    
                    # Read Gyro and convert to deg/s
                    gx = float(parts[idx+3]) / GYRO_SCALE
                    gy = float(parts[idx+4]) / GYRO_SCALE
                    
                    # 1. Calculate Accel Angles (in degrees)
                    # Note: Y and X mapping depends on physical orientation of the chip.
                    # Standard aerospace maps Y to Pitch and X to Roll.
                    accel_pitch = math.degrees(math.atan2(-ax, math.sqrt(ay*ay + az*az)))
                    accel_roll = math.degrees(math.atan2(ay, az))
                    
                    # 2. Apply Complementary Filter
                    # We integrate the gyroscope rate (deg/s * seconds = degrees)
                    pitch[i] = ALPHA * (pitch[i] + gy * dt) + (1.0 - ALPHA) * accel_pitch
                    roll[i]  = ALPHA * (roll[i]  + gx * dt) + (1.0 - ALPHA) * accel_roll
                
                updated = True
                
            except ValueError:
                pass

    if updated:
        # Redraw the 3D planes with their new pitch and roll angles
        for i in range(3):
            R = get_rotation_matrix(pitch[i], roll[i])
            
            # Apply rotation matrix to the base square coordinates
            rotated_square = base_square @ R.T
            
            # Update the polygon vertices
            polys[i].set_verts([rotated_square])
            
            # Update title to show numerical degrees
            axes[i].set_title(f"IMU {i+1}\nPitch: {pitch[i]:.1f}° | Roll: {roll[i]:.1f}°")

    return polys

# Interval=50 gives ~20fps. Blit=False required for 3D plots in matplotlib.
try:
    ani = animation.FuncAnimation(fig, update, interval=50, blit=False, save_count=100)
    plt.show()
except KeyboardInterrupt:
    pass
finally:
    ser.close()
    print("Serial port closed.")
