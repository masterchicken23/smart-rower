/**
 * Response shapes served by the Jetson's funnel (embedded/funnel), mirrored from
 * funnel/models.py. The funnel's /docs and /openapi.json are the source of truth;
 * keep these in step with it rather than adding fields the funnel does not send.
 */

/** `/rower/{seat}/position`, and the `position` block inside each seat in `/snapshot`. Pinned schema. */
export type SeatPosition = {
  /** 0.0 at the catch, 1.0 at the finish. Null while uncalibrated or the normalize stage is unimplemented. */
  position: number | null;
  /** Millimetres from the catch. While `calibrated` is false this is the sensor's raw distance, unreferenced. */
  travel_mm: number | null;
  velocity_mms: number | null;
  /** Source timestamp (unix seconds) of the newest input sample. */
  t: number | null;
  age_ms: number | null;
  stale: boolean;
  /** Input samples the pipeline saw this tick. */
  n_in: number;
  calibrated: boolean;
  /** False while any pipeline stage is still an identity no-op. */
  ready: boolean;
  /** Pipeline stages not yet implemented. */
  pending: string[];
};

/** A raw stream block (`/rower/{seat}/{stream}`): the sensor's payload passed through untouched. */
export type StreamBlock = {
  age_ms: number | null;
  rate_hz: number | null;
  stale: boolean;
  n: number;
  n_gap: number;
  seq: number | null;
  values: Record<string, unknown>;
  t: number | null;
  dev?: string;
};

/** One detection inside an AprilTag record. Camera frame is OpenCV: +x right, +y down, +z out of the lens. */
export type AprilTag = {
  id: number;
  fam: string;
  size_m: number;
  /** Tag centre in the camera frame, metres. */
  pos_m: [number, number, number];
  /** Rotation [w, x, y, z], tag frame → camera frame. */
  quat: [number, number, number, number];
  /** [rx, ry, rz], display only. */
  euler_deg: [number, number, number];
  dist_m: number;
  /** Tag centre in the corrected image, pixels from the top-left. */
  center_px: [number, number];
  corners_px: [number, number][];
  hamming: number;
  margin: number | null;
  err_px: number;
  /** Near 1, the rotation is unreliable. */
  ambiguity: number;
};

/**
 * The `values` of a seat's raw `apriltag` stream block: one detector record, untouched.
 * Contract: vision/april-detect/README.md, "Output contract". Sent for every frame, even with no tag (`n: 0`).
 */
export type AprilTagRecord = {
  type: 'apriltag';
  v: number;
  cam: string;
  tick: number;
  seq: number | null;
  skipped: number;
  t: number;
  clock: string;
  /** Image size in pixels. */
  w: number;
  h: number;
  ts: Record<string, number | null>;
  ms: Record<string, number | null>;
  n: number;
  tags: AprilTag[];
};

/** From a sensor's retained `rower/<seat>/status` topic. `up: false` is its MQTT Last Will. */
export type Presence = {
  up?: boolean;
  dev?: string;
  fw?: string;
  hz?: number;
  age_ms: number | null;
  [key: string]: unknown;
};

export type StreamStats = {
  path: string;
  scope: string;
  ident: number | null;
  stream: string;
  age_ms: number | null;
  rate_hz: number | null;
  stale: boolean;
  n_recv: number;
  n_bad: number;
  n_gap: number;
  buffered: number;
  last_seq: number | null;
  dev: string | null;
};

/** `/snapshot`. JSON object keys are strings, so seat numbers arrive as "1", "2", … */
export type SnapshotResponse = {
  tick: number;
  t: number;
  age_ms: number;
  /** Per seat: raw stream blocks by stream name, plus derived blocks such as `position`. */
  rowers: Record<string, Record<string, unknown>>;
  boat: Record<string, unknown>;
  streams: Record<string, Omit<StreamStats, 'path'> & { path?: string }>;
  presence: Record<string, Presence>;
};

export type TickStats = {
  tick: number;
  hz: number;
  jitter_ms: number;
  jitter_ms_max: number;
  n_late: number;
  n_errors: number;
  compute_ms: number;
};

/** `/health`. Answers 503 with this same body when no stream is fresh. */
export type HealthResponse = {
  status: 'ok' | 'stale' | 'no-data';
  uptime_s: number;
  tick: TickStats;
  sources: Record<string, Record<string, unknown>>;
  streams: StreamStats[];
  max_age_ms: number;
  recording: string | null;
  /** Pipeline name → stage name → "identity" (not implemented) or "active". */
  pipelines: Record<string, Record<string, string>>;
  unknown_topics?: number;
};

/** `X-Funnel-Status` on `/snapshot`. */
export type SnapshotStatus = 'ok' | 'starting' | 'stale-tick';

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

export function isSnapshot(v: unknown): v is SnapshotResponse {
  return isObj(v) && typeof v.tick === 'number' && isObj(v.rowers) && isObj(v.presence);
}

export function isHealth(v: unknown): v is HealthResponse {
  return isObj(v) && typeof v.status === 'string' && isObj(v.tick) && Array.isArray(v.streams);
}

export function isSeatPosition(v: unknown): v is SeatPosition {
  return isObj(v) && 'travel_mm' in v && typeof v.stale === 'boolean' && Array.isArray(v.pending);
}

export function isStreamBlock(v: unknown): v is StreamBlock {
  return isObj(v) && isObj(v.values) && typeof v.stale === 'boolean';
}

export function isAprilTagRecord(v: unknown): v is AprilTagRecord {
  return isObj(v) && v.type === 'apriltag' && typeof v.w === 'number' && typeof v.h === 'number' && Array.isArray(v.tags);
}
