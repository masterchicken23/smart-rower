import type { CatchSide } from './data/types';

// Coxswain view.
/** Seats shown before the funnel has reported any. The crew then grows to the highest seat it reports. */
export const DEFAULT_CREW_SIZE = 2;
/** Under this many ms from stroke, a seat counts as in sync. */
export const SYNC_THRESHOLD_MS = 25;
/** The SYNC box shows the median offset of this many recent strokes, damping tick-timing jitter. */
export const SYNC_SMOOTHING_STROKES = 3;
/** Normalized seat distance under which a lane's gap bar shows as in sync. */
export const RAIL_GAP_TOLERANCE = 0.05;

// Stroke detection on the funnel's normalized position, until the funnel computes strokes itself.
export const STROKE_LOW_MARK = 0.25;
export const STROKE_HIGH_MARK = 0.75;

// Funnel: the Jetson's REST service for sensor data (embedded/funnel). One per boat.
/** HTTP base of the funnel, e.g. http://10.88.164.133:8000. Can be changed at runtime on the Developer screen; both screens use it. */
export const FUNNEL_URL = process.env.EXPO_PUBLIC_FUNNEL_URL ?? 'http://10.88.164.133:8000';
/** The funnel recomputes at 10 Hz, so polling faster only returns the same tick again. */
export const FUNNEL_POLL_MS = 100;
export const FUNNEL_HEALTH_MS = 1000;
export const FUNNEL_TIMEOUT_MS = 1000;
export const FUNNEL_RETRY_MS = 1000;
/** How often the Developer log prints each seat's readings. */
export const FUNNEL_SEAT_LOG_MS = 1000;
/** How much history each seat trace shows. Long enough for several strokes. */
export const SEAT_TRACE_WINDOW_MS = 10_000;
/** Smallest vertical span a raw-mm trace is autoscaled to, so a still seat doesn't magnify noise. */
export const SEAT_TRACE_MIN_SPAN_MM = 40;

// Developer screen.
export const LOG_CAPACITY = 500;
/** How long a tag's fading trail on the AprilTag screen lasts, on the camera's capture clock. Display only. */
export const APRILTAG_TRAIL_MS = 3000;
/** Opacity of the newest trail segment; older segments fade to nothing. */
export const APRILTAG_TRAIL_OPACITY = 0.3;

export const DEFAULT_CATCH_SIDE: CatchSide = 'Left';
