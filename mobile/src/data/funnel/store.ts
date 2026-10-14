import { FUNNEL_SEAT_LOG_MS, FUNNEL_URL, LOG_CAPACITY, SEAT_TRACE_WINDOW_MS } from '../../config';
import { Slice } from '../store';
import type { LogKind, LogLine } from '../types';
import { FunnelClient } from './client';
import {
  isSeatPosition,
  isStreamBlock,
  type HealthResponse,
  type Presence,
  type SeatPosition,
  type SnapshotResponse,
  type SnapshotStatus,
  type StreamBlock,
  type StreamStats,
} from './types';

/** The raw MQTT stream the seat-position pipeline consumes (funnel: seat_position.RAW_STREAM). */
export const SEAT_STREAM = 'seat';
/** The raw stream from the camera watching a seat (funnel: sources/zmq_apriltag.STREAM). */
export const APRILTAG_STREAM = 'apriltag';
/** Derived block names inside a seat. Everything else in a seat is a raw stream. */
const DERIVED_BLOCKS = new Set(['position']);

export type ConnState = 'idle' | 'connecting' | 'ok' | 'starting' | 'stale-tick' | 'unreachable';

export type Connection = {
  state: ConnState;
  /** Round trip of the last successful /snapshot. */
  rttMs: number | null;
  tick: number | null;
  /** How old the funnel says its snapshot was when it answered. */
  snapshotAgeMs: number | null;
  error: string | null;
};

/** One point of a seat's history. `t` is the funnel tick's wall clock in seconds; nulls are gaps. */
export type TracePoint = { t: number; position: number | null; travelMm: number | null; sourceT: number | null };

export type SeatView = {
  seat: number;
  /** The derived `position` block; null if the seat has never published its seat stream. */
  position: SeatPosition | null;
  /** The raw `seat` stream block, exactly as the sensor published it. */
  raw: StreamBlock | null;
  /** The raw `apriltag` stream block; its `values` is the detector's record. Null without a camera. */
  apriltag: StreamBlock | null;
  /** Every raw stream this seat publishes. */
  streams: string[];
  presence: Presence | null;
  /** Receive and drop counters for the raw seat stream. */
  stats: StreamStats | null;
  history: TracePoint[];
};

const INITIAL_CONNECTION: Connection = { state: 'idle', rttMs: null, tick: null, snapshotAgeMs: null, error: null };

type SeatMemo = {
  stale: boolean | null;
  up: boolean | undefined;
  nGap: number;
  calibrated: boolean | null;
  ready: boolean | null;
  lastLogAt: number;
};

const pad = (n: number, l = 2) => String(n).padStart(l, '0');
function timestamp(d = new Date()) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

const isNum = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n);
/** Formats a nullable number; null is shown as an em dash, never as 0. */
export function fmt(n: unknown, digits = 0, unit = '') {
  return isNum(n) ? `${n.toFixed(digits)}${unit}` : '—';
}

/** Accepts "10.0.0.5:8000" or a full URL; returns it without a trailing slash. */
export function normalizeUrl(input: string) {
  const s = input.trim().replace(/\/+$/, '');
  if (!s) return '';
  return /^https?:\/\//i.test(s) ? s : `http://${s}`;
}

function describeValues(values: Record<string, unknown>) {
  return Object.entries(values)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join(' ');
}

/** Called once per new funnel tick. `t` is the tick's wall clock in unix seconds, shared by every seat. */
export type TickListener = (t: number, seats: SeatView[]) => void;

/** Live view of the funnel, shared by the Developer and Coxswain screens. Shows only what the funnel sends. */
export class FunnelStore {
  readonly url = new Slice<string>(normalizeUrl(FUNNEL_URL));
  readonly connection = new Slice<Connection>(INITIAL_CONNECTION);
  readonly seats = new Slice<SeatView[]>([]);
  readonly health = new Slice<HealthResponse | null>(null);
  readonly log = new Slice<LogLine[]>([]);

  private client: FunnelClient | null = null;
  private users = 0;
  private tickListeners = new Set<TickListener>();
  private lastTick: number | null = null;
  private memos = new Map<number, SeatMemo>();
  private histories = new Map<number, TracePoint[]>();
  private healthFailing = false;
  private lastHealthStatus: string | null = null;
  private lastPipelines = '';
  private lastUnknownTopics = 0;
  private sourceConnected = new Map<string, unknown>();
  private badCounts = new Map<string, number>();
  private logBuffer: LogLine[] = [];
  private logSeq = 0;
  private logFlush: ReturnType<typeof setTimeout> | null = null;

  /** Polls while at least one screen holds the store. Returns the release function. */
  retain() {
    this.users += 1;
    this.start();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.users -= 1;
      if (this.users === 0) this.stop();
    };
  }

  onTick(listener: TickListener) {
    this.tickListeners.add(listener);
    return () => {
      this.tickListeners.delete(listener);
    };
  }

  private start() {
    if (this.client) return;
    const base = this.url.get();
    if (!base) {
      this.append('net', 'no funnel URL set (EXPO_PUBLIC_FUNNEL_URL)', 'warn');
      return;
    }
    this.connection.set({ ...INITIAL_CONNECTION, state: 'connecting' });
    this.append('net', `polling ${base}/snapshot and /health`, 'sys');
    this.client = new FunnelClient(base, {
      snapshot: this.onSnapshot,
      health: this.onHealth,
      error: this.onError,
    });
    this.client.start();
  }

  private stop() {
    this.client?.stop();
    this.client = null;
    this.connection.set(INITIAL_CONNECTION);
  }

  setUrl(input: string) {
    const next = normalizeUrl(input);
    if (!next || next === this.url.get()) return;
    const running = this.client !== null;
    this.stop();
    this.url.set(next);
    this.resetData();
    if (running) this.start();
  }

  clearLog() {
    this.logBuffer = [];
    this.log.set([]);
  }

  private resetData() {
    this.lastTick = null;
    this.memos.clear();
    this.histories.clear();
    this.seats.set([]);
    this.health.set(null);
    this.healthFailing = false;
    this.lastHealthStatus = null;
    this.lastPipelines = '';
    this.lastUnknownTopics = 0;
    this.sourceConnected.clear();
    this.badCounts.clear();
  }

  // ---- Snapshot ----

  private onSnapshot = (body: SnapshotResponse, status: SnapshotStatus, rttMs: number) => {
    const prev = this.connection.get();
    if (prev.state !== status) {
      if (status === 'ok') this.append('net', `connected to ${this.url.get()}  rtt=${rttMs}ms  tick=${body.tick}`, 'sync');
      else if (status === 'starting') this.append('net', 'funnel is up, waiting for its first compute tick', 'sys');
      else this.append('net', `funnel compute loop stalled at tick ${body.tick} (${fmt(body.age_ms)}ms old)`, 'warn');
    }
    this.connection.set({ state: status, rttMs, tick: body.tick, snapshotAgeMs: body.age_ms, error: null });

    if (body.tick === this.lastTick) return;
    if (this.lastTick !== null && body.tick < this.lastTick) {
      this.append('net', `tick went ${this.lastTick} → ${body.tick}: funnel restarted`, 'warn');
      this.memos.clear();
      this.histories.clear();
    }
    this.lastTick = body.tick;
    if (body.tick === 0) return;

    const seatNums = new Set<number>();
    for (const k of [...Object.keys(body.rowers), ...Object.keys(body.presence)]) {
      const n = Number(k);
      if (Number.isInteger(n) && n > 0) seatNums.add(n);
    }

    const now = Date.now();
    const views: SeatView[] = [];
    for (const seat of [...seatNums].sort((a, b) => a - b)) {
      const blocks = body.rowers[String(seat)] ?? {};
      const position = isSeatPosition(blocks.position) ? blocks.position : null;
      const raw = isStreamBlock(blocks[SEAT_STREAM]) ? blocks[SEAT_STREAM] : null;
      const apriltag = isStreamBlock(blocks[APRILTAG_STREAM]) ? blocks[APRILTAG_STREAM] : null;
      const presence = body.presence[String(seat)] ?? null;
      const s = body.streams[`rower/${seat}/${SEAT_STREAM}`];
      const stats = s ? ({ path: `rower/${seat}/${SEAT_STREAM}`, ...s } as StreamStats) : null;
      const streams = Object.keys(blocks).filter((k) => !DERIVED_BLOCKS.has(k));

      const history = this.pushHistory(seat, body.t, position);
      const view: SeatView = { seat, position, raw, apriltag, streams, presence, stats, history };
      this.logSeat(view, now);
      views.push(view);
    }
    this.seats.set(views);
    this.tickListeners.forEach((l) => l(body.t, views));
  };

  private pushHistory(seat: number, t: number, p: SeatPosition | null) {
    const prev = this.histories.get(seat) ?? [];
    const cutoff = t - SEAT_TRACE_WINDOW_MS / 1000;
    const next = prev.filter((x) => x.t >= cutoff);
    const live = p !== null && !p.stale;
    if (!live) {
      // A stale reading is a gap, not a value.
      next.push({ t, position: null, travelMm: null, sourceT: null });
    } else if (p.t === null || p.t !== prev[prev.length - 1]?.sourceT) {
      // Ticks that repeat the previous sample (sensor slower than the tick, or about to go
      // stale) add nothing, so the line ends at the last real reading instead of going flat.
      next.push({
        t,
        position: isNum(p.position) ? p.position : null,
        travelMm: isNum(p.travel_mm) ? p.travel_mm : null,
        sourceT: p.t,
      });
    }
    this.histories.set(seat, next);
    return next;
  }

  private logSeat(v: SeatView, now: number) {
    const src = `s${v.seat}`;
    let m = this.memos.get(v.seat);
    if (!m) {
      m = { stale: null, up: undefined, nGap: v.stats?.n_gap ?? 0, calibrated: null, ready: null, lastLogAt: 0 };
      this.memos.set(v.seat, m);
      const dev = v.raw?.dev ?? v.presence?.dev;
      this.append(
        src,
        `seat ${v.seat} discovered  streams=[${v.streams.join(', ') || 'none'}]${dev ? `  dev=${dev}` : ''}`,
        'sys',
      );
    }

    const up = v.presence?.up;
    if (up !== m.up && up !== undefined) {
      const p = v.presence!;
      if (up) {
        const extra = [p.dev && `dev=${p.dev}`, p.fw && `fw=${p.fw}`, isNum(p.hz) && `${p.hz}Hz`].filter(Boolean);
        this.append(src, `sensor up  ${extra.join('  ')}`, 'sync');
      } else {
        this.append(src, 'sensor down (MQTT last will: disconnected from broker)', 'warn');
      }
    }
    m.up = up;

    const p = v.position;
    if (!p) {
      if (m.stale !== true && v.streams.length === 0 && m.lastLogAt === 0) {
        this.append(src, `known from its status topic only; nothing on rower/${v.seat}/${SEAT_STREAM} yet`, 'warn');
        m.lastLogAt = now;
      }
      return;
    }

    if (m.stale !== p.stale) {
      if (p.stale) this.append(src, `stale: no sample for ${fmt(p.age_ms)}ms`, 'warn');
      else if (m.stale !== null) this.append(src, 'fresh again', 'sync');
      m.stale = p.stale;
    }

    const gap = v.stats?.n_gap ?? 0;
    if (gap < m.nGap) m.nGap = gap;

    if (m.calibrated !== p.calibrated) {
      if (m.calibrated !== null || p.calibrated) this.append(src, p.calibrated ? 'calibrated' : 'calibration lost', 'sys');
      m.calibrated = p.calibrated;
    }
    if (m.ready !== p.ready) {
      if (m.ready !== null || p.ready) this.append(src, p.ready ? 'pipeline ready' : `pipeline pending: ${p.pending.join(', ')}`, 'sys');
      m.ready = p.ready;
    }

    if (p.stale || now - m.lastLogAt < FUNNEL_SEAT_LOG_MS) return;
    m.lastLogAt = now;
    const lost = gap - m.nGap;
    m.nGap = gap;
    const parts = [
      `travel=${fmt(p.travel_mm, 0, 'mm')}`,
      `pos=${fmt(p.position, 3)}`,
      `v=${fmt(p.velocity_mms, 0)}`,
      fmt(v.raw?.rate_hz ?? null, 1, 'Hz'),
      `age=${fmt(p.age_ms, 0, 'ms')}`,
      `gap=${gap}${lost > 0 ? `(+${lost})` : ''}`,
      `n_in=${p.n_in}`,
    ];
    if (v.raw) parts.push(`raw{${describeValues(v.raw.values)}}`);
    this.append(src, parts.join('  '), lost > 0 ? 'warn' : 'normal');
  }

  // ---- Health ----

  private onHealth = (body: HealthResponse, httpStatus: number) => {
    this.health.set(body);
    if (this.healthFailing) {
      this.healthFailing = false;
      this.append('fnl', '/health reachable again', 'sys');
    }

    if (body.status !== this.lastHealthStatus) {
      const msg =
        body.status === 'ok'
          ? `health ok  uptime=${Math.round(body.uptime_s)}s  ${body.tick.hz}Hz compute`
          : body.status === 'stale'
            ? `health stale (HTTP ${httpStatus}): every stream has gone quiet`
            : `health no-data (HTTP ${httpStatus}): no sensor has published yet`;
      this.append('fnl', msg, body.status === 'ok' ? 'sync' : 'warn');
      this.lastHealthStatus = body.status;
    }

    const pipelines = JSON.stringify(body.pipelines);
    if (pipelines !== this.lastPipelines) {
      for (const [name, stages] of Object.entries(body.pipelines)) {
        const list = Object.entries(stages)
          .map(([stage, state]) => `${stage}=${state}`)
          .join(' ');
        this.append('fnl', `pipeline ${name}: ${list}`, 'sys');
      }
      this.lastPipelines = pipelines;
    }

    for (const [name, stats] of Object.entries(body.sources)) {
      if (!('connected' in stats)) continue;
      const was = this.sourceConnected.get(name);
      if (was === stats.connected) continue;
      this.sourceConnected.set(name, stats.connected);
      const where = typeof stats.broker === 'string' ? ` ${stats.broker}` : '';
      if (stats.connected) this.append('fnl', `source ${name}${where} connected`, 'sync');
      else {
        const err = stats.last_error ? `: ${String(stats.last_error)}` : '';
        this.append('fnl', `source ${name}${where} not connected${err}`, 'warn');
      }
    }

    const unknown = body.unknown_topics ?? 0;
    if (unknown > this.lastUnknownTopics) {
      this.append('fnl', `${unknown} message(s) on topics the funnel doesn't recognise`, 'warn');
    }
    this.lastUnknownTopics = unknown;

    for (const s of body.streams) {
      if (s.n_bad > 0 && s.n_bad !== this.badCounts.get(s.path)) {
        this.append('fnl', `${s.path}: ${s.n_bad} payload(s) failed to decode`, 'warn');
        this.badCounts.set(s.path, s.n_bad);
      }
    }
  };

  // ---- Errors ----

  private onError = (path: string, reason: string) => {
    if (path === '/health') {
      // Only worth a line when the snapshot is fine; otherwise the snapshot error says it already.
      if (!this.healthFailing && this.connection.get().state === 'ok') {
        this.append('fnl', `GET /health failed: ${reason}`, 'warn');
      }
      this.healthFailing = true;
      return;
    }
    const prev = this.connection.get();
    if (prev.state !== 'unreachable' || prev.error !== reason) {
      this.append('net', `GET ${this.url.get()}${path}: ${reason}`, 'warn');
    }
    this.connection.set({ ...prev, state: 'unreachable', rttMs: null, error: reason });
  };

  // ---- Log ----

  /** Adds a line to the funnel log; the Coxswain screen uses it for the sync it measures. */
  append(src: string, msg: string, kind: LogKind) {
    this.logBuffer.push({ id: ++this.logSeq, t: timestamp(), src, msg, kind });
    if (this.logBuffer.length > LOG_CAPACITY) {
      this.logBuffer.splice(0, this.logBuffer.length - LOG_CAPACITY);
    }
    this.logFlush ??= setTimeout(() => {
      this.logFlush = null;
      this.log.set(this.logBuffer.slice());
    }, 100);
  }
}
