export type HandState = 'good' | 'raise' | 'lower';
export type SyncPoint = 'CATCH' | 'FINISH';
export type SyncFeedback = { kind: 'in' | 'early' | 'late'; point: SyncPoint; ms: number };
export type CatchSide = 'Left' | 'Right';

/**
 * One seat in the boat. `seat` is the funnel's seat number (bow = 1, stroke = crew size).
 * The labels follow the coxswain design, which counts rowers from stroke.
 */
export type Seat = {
  seat: number;
  /** STROKE, 3 SEAT, 2 SEAT, BOW. */
  name: string;
  /** Dot letter: S, 3, 2, B. */
  short: string;
  /** "Rower 1" is stroke. */
  sub: string;
  isStroke: boolean;
};

/**
 * Whether a seat has a current position to show.
 * - `live`: fresh, calibrated `position`.
 * - `calibrating`: the seat is publishing but hasn't completed its first full stroke.
 * - `no-signal`: the seat is unknown, stale, or has no reading this tick.
 * - `offline`: the funnel itself isn't answering.
 */
export type SeatSignal = 'live' | 'calibrating' | 'no-signal' | 'offline';

export type SeatFeedback = {
  /** Null until a hand/oar-height feed exists on the Jetson. */
  hands: HandState | null;
  /** Offset from stroke, measured once per stroke. Null for stroke and before the first measured stroke. */
  sync: SyncFeedback | null;
};

export type SessionState = {
  running: boolean;
  /** Wall-clock time the current running stretch began, or null while paused/idle. */
  runningSince: number | null;
  /** Elapsed time banked from earlier running stretches. */
  bankedMs: number;
  rateSpm: number | null;
  started: boolean;
};

export type LogKind = 'normal' | 'sync' | 'warn' | 'sys';
/** `src` is a short seat or subsystem tag, e.g. `s3`, `net`. */
export type LogLine = { id: number; t: string; src: string; msg: string; kind: LogKind };
