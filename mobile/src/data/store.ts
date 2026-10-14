import { Easing, makeMutable, withTiming, type SharedValue } from 'react-native-reanimated';

import {
  DEFAULT_CATCH_SIDE,
  DEFAULT_CREW_SIZE,
  FUNNEL_POLL_MS,
  SYNC_SMOOTHING_STROKES,
  SYNC_THRESHOLD_MS,
} from '../config';
import type { ConnState, FunnelStore, SeatView } from './funnel/store';
import { StrokeDetector, type DetectedStroke } from './strokeDetector';
import type {
  CatchSide,
  Seat,
  SeatFeedback,
  SeatSignal,
  SessionState,
  SyncFeedback,
  SyncPoint,
} from './types';

type Listener = () => void;

/** A piece of immutable state that React can subscribe to with useSyncExternalStore. */
export class Slice<T> {
  private listeners = new Set<Listener>();
  constructor(private value: T) {}
  get = () => this.value;
  set(next: T) {
    if (next === this.value) return;
    this.value = next;
    this.listeners.forEach((l) => l());
  }
  update(fn: (prev: T) => T) {
    this.set(fn(this.value));
  }
  subscribe = (l: Listener) => {
    this.listeners.add(l);
    return () => {
      this.listeners.delete(l);
    };
  };
}

/** One detected stroke. `matchedWith` holds the seats it has been paired with for sync. */
type StrokeRecord = { catchT: number; finishT: number; periodMs: number; matchedWith: Set<number> };

type SeatTracker = {
  detector: StrokeDetector;
  strokes: StrokeRecord[];
  offsets: Record<SyncPoint, number[]>;
};

const INITIAL_SESSION: SessionState = {
  running: false,
  runningSince: null,
  bankedMs: 0,
  rateSpm: null,
  started: false,
};

/** Assumed stroke period for pairing strokes before a seat has two catches to measure. */
const FALLBACK_PERIOD_MS = 2000;
/** The funnel serves the same reading to every request within a tick; these bound the glide between ticks. */
const MIN_GLIDE_MS = 50;
const MAX_GLIDE_MS = 250;

/** Seats ordered stroke first, towards the bow. Funnel seats count from the bow, so stroke is `size`. */
export function crewFor(size: number): Seat[] {
  const seats: Seat[] = [];
  for (let n = size; n >= 1; n--) {
    const isStroke = n === size;
    const isBow = n === 1 && !isStroke;
    seats.push({
      seat: n,
      name: isStroke ? 'STROKE' : isBow ? 'BOW' : `${n} SEAT`,
      short: isStroke ? 'S' : isBow ? 'B' : String(n),
      sub: `Rower ${size - n + 1}`,
      isStroke,
    });
  }
  return seats;
}

export function elapsedMs(s: SessionState, now = Date.now()) {
  return s.bankedMs + (s.running && s.runningSince !== null ? now - s.runningSince : 0);
}

export function classifySync(point: SyncPoint, ms: number): SyncFeedback {
  const kind = Math.abs(ms) < SYNC_THRESHOLD_MS ? 'in' : ms > 0 ? 'late' : 'early';
  return { kind, point, ms: Math.round(ms) };
}

function median(xs: number[]) {
  const s = [...xs].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

const signed = (n: number) => `${n > 0 ? '+' : ''}${Math.round(n)}`;

function signalOf(view: SeatView | undefined): SeatSignal {
  const p = view?.position;
  if (!p || p.stale) return 'no-signal';
  if (p.position === null) return p.calibrated ? 'no-signal' : 'calibrating';
  return 'live';
}

const OFFLINE: ConnState[] = ['unreachable', 'stale-tick'];

const newTracker = (): SeatTracker => ({ detector: new StrokeDetector(), strokes: [], offsets: { CATCH: [], FINISH: [] } });

/**
 * Coxswain state, fed by the funnel's `/snapshot`. Seat positions come straight from the
 * funnel's `position` block. Strokes, rate and sync are timed here on the funnel's tick clock,
 * which every seat shares, until the funnel computes them itself (JETSON_API.md §11).
 */
export class RowingStore {
  readonly session = new Slice<SessionState>(INITIAL_SESSION);
  readonly crew = new Slice<Seat[]>(crewFor(DEFAULT_CREW_SIZE));
  readonly signal = new Slice<Record<number, SeatSignal>>({});
  readonly feedback = new Slice<Record<number, SeatFeedback>>({});
  readonly catchSide = new Slice<CatchSide>(DEFAULT_CATCH_SIDE);

  /** Normalized seat position (0 catch … 1 finish) per funnel seat, frozen while paused. */
  private positions = new Map<number, SharedValue<number>>();
  private trackers = new Map<number, SeatTracker>();
  private lastTickT: number | null = null;
  private glideMs = FUNNEL_POLL_MS;

  constructor(private readonly funnel: FunnelStore) {}

  /** Starts listening to the funnel while the Coxswain screen is open. Returns the detach function. */
  attach() {
    const release = this.funnel.retain();
    const offTick = this.funnel.onTick(this.onTick);
    const offConn = this.funnel.connection.subscribe(this.onConnection);
    this.onConnection();
    return () => {
      offTick();
      offConn();
      release();
      this.lastTickT = null;
    };
  }

  position(seat: number): SharedValue<number> {
    let v = this.positions.get(seat);
    if (!v) {
      v = makeMutable(0);
      this.positions.set(seat, v);
    }
    return v;
  }

  // ---- Actions ----

  beginSession() {
    const s = this.session.get();
    if (s.started) return;
    this.session.set({ ...s, started: true, running: true, runningSince: Date.now() });
  }

  togglePause() {
    const s = this.session.get();
    const now = Date.now();
    if (s.running) {
      this.session.set({ ...s, running: false, runningSince: null, bankedMs: elapsedMs(s, now) });
    } else {
      this.session.set({ ...s, started: true, running: true, runningSince: now });
    }
  }

  reset() {
    const s = this.session.get();
    this.session.set({ ...s, bankedMs: 0, runningSince: s.running ? Date.now() : null });
    this.feedback.set({});
    this.trackers.forEach((tr) => (tr.offsets = { CATCH: [], FINISH: [] }));
    this.funnel.append('cox', 'session reset', 'sys');
  }

  toggleCatchSide() {
    this.catchSide.update((c) => (c === 'Left' ? 'Right' : 'Left'));
  }

  // ---- Funnel ----

  private onConnection = () => {
    if (!OFFLINE.includes(this.funnel.connection.get().state)) return;
    const next: Record<number, SeatSignal> = {};
    for (const s of this.crew.get()) next[s.seat] = 'offline';
    this.setSignals(next);
    this.trackers.forEach((tr) => (tr.detector = new StrokeDetector()));
  };

  private onTick = (t: number, views: SeatView[]) => {
    const size = Math.max(DEFAULT_CREW_SIZE, ...views.map((v) => v.seat));
    if (size !== this.crew.get().length) this.crew.set(crewFor(size));

    if (this.lastTickT !== null) {
      const dt = Math.min(MAX_GLIDE_MS, Math.max(MIN_GLIDE_MS, (t - this.lastTickT) * 1000));
      this.glideMs += (dt - this.glideMs) * 0.2;
    }
    this.lastTickT = t;

    const running = this.session.get().running;
    const byNum = new Map(views.map((v) => [v.seat, v]));
    const signals: Record<number, SeatSignal> = {};
    for (let seat = 1; seat <= size; seat++) {
      const view = byNum.get(seat);
      const signal = signalOf(view);
      signals[seat] = signal;
      const tr = this.tracker(seat);
      if (signal !== 'live') {
        // A gap would otherwise be interpolated across as if it were part of a stroke.
        tr.detector = new StrokeDetector();
        continue;
      }
      const pos = view!.position!.position!;
      // Glide to each tick's reading over one tick so the dots move at display rate.
      if (running) this.position(seat).value = withTiming(pos, { duration: this.glideMs, easing: Easing.linear });

      const stroke = tr.detector.push(t * 1000, pos);
      if (stroke) this.onStroke(seat, size, stroke);
    }
    this.setSignals(signals);
  };

  private tracker(seat: number) {
    let tr = this.trackers.get(seat);
    if (!tr) {
      tr = newTracker();
      this.trackers.set(seat, tr);
    }
    return tr;
  }

  private setSignals(next: Record<number, SeatSignal>) {
    const prev = this.signal.get();
    const keys = new Set([...Object.keys(prev), ...Object.keys(next)]);
    for (const k of keys) {
      if (prev[Number(k)] !== next[Number(k)]) {
        this.signal.set(next);
        return;
      }
    }
  }

  // ---- Strokes and sync ----

  private onStroke(seat: number, strokeSeat: number, s: DetectedStroke) {
    const tr = this.tracker(seat);
    tr.strokes.push({
      catchT: s.catchT,
      finishT: s.finishT,
      periodMs: s.periodMs ?? FALLBACK_PERIOD_MS,
      matchedWith: new Set(),
    });
    if (tr.strokes.length > 4) tr.strokes.shift();

    const session = this.session.get();
    if (seat === strokeSeat && session.running && s.periodMs !== null) {
      this.session.set({ ...session, rateSpm: Math.round(60000 / s.periodMs) });
    }
    this.matchStrokes(strokeSeat);
  }

  /** Pairs each seat's strokes with stroke's stroke whose catch is closest, within half a stroke. */
  private matchStrokes(strokeSeat: number) {
    const reference = this.trackers.get(strokeSeat)?.strokes ?? [];
    for (const [seat, tr] of this.trackers) {
      if (seat === strokeSeat) continue;
      for (const rec of tr.strokes) {
        if (rec.matchedWith.has(strokeSeat)) continue;
        let best: StrokeRecord | null = null;
        for (const ref of reference) {
          if (ref.matchedWith.has(seat)) continue;
          const d = Math.abs(rec.catchT - ref.catchT);
          if (d < ref.periodMs / 2 && (!best || d < Math.abs(rec.catchT - best.catchT))) best = ref;
        }
        if (!best) continue;
        rec.matchedWith.add(strokeSeat);
        best.matchedWith.add(seat);
        this.recordOffsets(seat, tr, rec.catchT - best.catchT, rec.finishT - best.finishT);
      }
    }
  }

  private recordOffsets(seat: number, tr: SeatTracker, catchMs: number, finishMs: number) {
    const push = (xs: number[], x: number) => {
      xs.push(x);
      if (xs.length > SYNC_SMOOTHING_STROKES) xs.shift();
    };
    push(tr.offsets.CATCH, catchMs);
    push(tr.offsets.FINISH, finishMs);

    // Report whichever point is further out of sync.
    const catchMed = median(tr.offsets.CATCH);
    const finishMed = median(tr.offsets.FINISH);
    const useFinish = Math.abs(finishMed) > Math.abs(catchMed);
    const sync = classifySync(useFinish ? 'FINISH' : 'CATCH', useFinish ? finishMed : catchMed);

    const verdict = sync.kind === 'in' ? 'IN SYNC' : `${sync.kind.toUpperCase()} ${sync.point}`;
    this.funnel.append(
      `s${seat}`,
      `vs stroke: catch ${signed(catchMs)}ms  finish ${signed(finishMs)}ms  → ${verdict} ${signed(sync.ms)}ms`,
      sync.kind === 'in' ? 'sync' : 'normal',
    );
    if (!this.session.get().running) return;
    this.feedback.update((f) => ({ ...f, [seat]: { hands: f[seat]?.hands ?? null, sync } }));
  }
}
