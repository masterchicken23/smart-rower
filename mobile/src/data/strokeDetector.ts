import { STROKE_HIGH_MARK, STROKE_LOW_MARK } from '../config';

export type DetectedStroke = {
  /** When the drive passed the low mark, just after the catch. */
  catchT: number;
  /** When the recovery passed the high mark, just after the finish. */
  finishT: number;
  periodMs: number | null;
};

type State = 'bottom' | 'drive' | 'top' | 'recovery';

/**
 * Times each stroke on a normalized 0–1 slide signal. Rather than locating the
 * flat turning points, it timestamps where the signal crosses a mark on the steep
 * part of the curve, interpolated between frames. Both rowers are timed at the
 * same marks, so the difference between them is the sync offset. The gap between
 * the two marks provides the hysteresis that stops pose jitter creating strokes.
 */
export class StrokeDetector {
  private state: State = 'recovery';
  private prev: { t: number; pos: number } | null = null;
  private catchT: number | null = null;
  private lastCatchT: number | null = null;

  push(t: number, pos: number): DetectedStroke | null {
    const prev = this.prev;
    this.prev = { t, pos };
    const crossing = (mark: number) =>
      prev && pos !== prev.pos ? prev.t + ((mark - prev.pos) / (pos - prev.pos)) * (t - prev.t) : t;

    switch (this.state) {
      case 'recovery':
        if (pos <= STROKE_LOW_MARK) this.state = 'bottom';
        return null;
      case 'bottom':
        if (pos > STROKE_LOW_MARK) {
          this.lastCatchT = this.catchT;
          this.catchT = crossing(STROKE_LOW_MARK);
          this.state = 'drive';
        }
        return null;
      case 'drive':
        if (pos >= STROKE_HIGH_MARK) this.state = 'top';
        return null;
      case 'top': {
        if (pos >= STROKE_HIGH_MARK) return null;
        this.state = 'recovery';
        if (this.catchT === null) return null;
        const period = this.lastCatchT !== null ? this.catchT - this.lastCatchT : null;
        return {
          catchT: this.catchT,
          finishT: crossing(STROKE_HIGH_MARK),
          periodMs: period !== null && period > 1000 && period < 6000 ? period : null,
        };
      }
    }
  }
}
