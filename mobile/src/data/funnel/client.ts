import { FUNNEL_HEALTH_MS, FUNNEL_POLL_MS, FUNNEL_RETRY_MS, FUNNEL_TIMEOUT_MS } from '../../config';
import { isHealth, isSnapshot, type HealthResponse, type SnapshotResponse, type SnapshotStatus } from './types';

export type FunnelEvents = {
  /** Every successful /snapshot, including repeats of a tick already seen. */
  snapshot(body: SnapshotResponse, status: SnapshotStatus, rttMs: number): void;
  /** /health answers 503 with a full body when no stream is fresh, so both codes carry one. */
  health(body: HealthResponse, httpStatus: number): void;
  /** A request that failed, timed out, or returned something that isn't the funnel's shape. */
  error(path: string, reason: string): void;
};

type Got = { status: number; body: unknown; text: string; headers: Headers };

async function getJson(url: string): Promise<Got> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FUNNEL_TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal, headers: { Accept: 'application/json' } });
    const text = await res.text();
    let body: unknown = null;
    try {
      body = JSON.parse(text);
    } catch {
      // Left as null; the caller reports it.
    }
    return { status: res.status, body, text, headers: res.headers };
  } finally {
    clearTimeout(timer);
  }
}

const brief = (s: string, n = 80) => (s.length > n ? `${s.slice(0, n)}…` : s);

function reasonOf(e: unknown) {
  if (e instanceof Error && e.name === 'AbortError') return `timeout (${FUNNEL_TIMEOUT_MS} ms)`;
  return e instanceof Error ? e.message : String(e);
}

function snapshotStatus(header: string | null): SnapshotStatus {
  return header === 'starting' || header === 'stale-tick' ? header : 'ok';
}

/**
 * Polls one funnel. It only fetches and validates; interpreting the data is the store's job.
 * Requests never overlap: each poll is scheduled after the previous one settles.
 */
export class FunnelClient {
  private stopped = true;
  private snapTimer: ReturnType<typeof setTimeout> | null = null;
  private healthTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    readonly baseUrl: string,
    private readonly on: FunnelEvents,
  ) {}

  start() {
    this.stopped = false;
    this.pollSnapshot();
    this.pollHealth();
  }

  stop() {
    this.stopped = true;
    if (this.snapTimer) clearTimeout(this.snapTimer);
    if (this.healthTimer) clearTimeout(this.healthTimer);
    this.snapTimer = this.healthTimer = null;
  }

  private pollSnapshot = async () => {
    if (this.stopped) return;
    const started = Date.now();
    let delay = FUNNEL_POLL_MS;
    try {
      const { status, body, text, headers } = await getJson(`${this.baseUrl}/snapshot`);
      if (this.stopped) return;
      if (status !== 200) {
        this.on.error('/snapshot', `HTTP ${status}`);
        delay = FUNNEL_RETRY_MS;
      } else if (!isSnapshot(body)) {
        this.on.error('/snapshot', `unexpected body: ${brief(text)}`);
        delay = FUNNEL_RETRY_MS;
      } else {
        this.on.snapshot(body, snapshotStatus(headers.get('X-Funnel-Status')), Date.now() - started);
      }
    } catch (e) {
      if (this.stopped) return;
      this.on.error('/snapshot', reasonOf(e));
      delay = FUNNEL_RETRY_MS;
    }
    this.snapTimer = setTimeout(this.pollSnapshot, Math.max(0, delay - (Date.now() - started)));
  };

  private pollHealth = async () => {
    if (this.stopped) return;
    const started = Date.now();
    try {
      const { status, body, text } = await getJson(`${this.baseUrl}/health`);
      if (this.stopped) return;
      if (isHealth(body)) this.on.health(body, status);
      else this.on.error('/health', `HTTP ${status}: ${brief(text)}`);
    } catch (e) {
      if (this.stopped) return;
      this.on.error('/health', reasonOf(e));
    }
    this.healthTimer = setTimeout(this.pollHealth, Math.max(0, FUNNEL_HEALTH_MS - (Date.now() - started)));
  };
}
