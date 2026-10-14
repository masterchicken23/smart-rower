import { useState } from 'react';
import { StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';
import Svg, { Circle, Path } from 'react-native-svg';

import { SEAT_TRACE_MIN_SPAN_MM, SEAT_TRACE_WINDOW_MS } from '@/config';
import { fmt, type SeatView, type TracePoint } from '@/data/funnel/store';
import { useScale, useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii, tabular } from '@/theme/tokens';

type Mode = 'position' | 'travel';
type Scale = { lo: number; hi: number };

/**
 * Plots `position` (0 catch … 1 finish) once the funnel sends it; until then the raw
 * `travel_mm`, autoscaled to the visible window. The autoscale is for display only —
 * it is not a calibration, and the card says so.
 */
function scaleFor(mode: Mode, points: TracePoint[]): Scale | null {
  if (mode === 'position') return { lo: 0, hi: 1 };
  let lo = Infinity;
  let hi = -Infinity;
  for (const p of points) {
    if (p.travelMm === null) continue;
    lo = Math.min(lo, p.travelMm);
    hi = Math.max(hi, p.travelMm);
  }
  if (lo > hi) return null;
  const span = Math.max(hi - lo, SEAT_TRACE_MIN_SPAN_MM);
  const mid = (hi + lo) / 2;
  return { lo: mid - span * 0.55, hi: mid + span * 0.55 };
}

function buildPath(points: TracePoint[], mode: Mode, sc: Scale, w: number, h: number) {
  if (!points.length || w <= 0) return { d: '', last: null as { x: number; y: number } | null };
  const tEnd = points[points.length - 1].t;
  const windowS = SEAT_TRACE_WINDOW_MS / 1000;
  let d = '';
  let pen = false;
  let last: { x: number; y: number } | null = null;
  for (const p of points) {
    const v = mode === 'position' ? p.position : p.travelMm;
    if (v === null) {
      pen = false; // a gap in the data is a gap in the line
      last = null;
      continue;
    }
    const x = w - ((tEnd - p.t) / windowS) * w;
    const y = (1 - (v - sc.lo) / (sc.hi - sc.lo)) * h;
    d += `${pen ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
    pen = true;
    last = { x, y };
  }
  return { d, last };
}

/** `offline`: the funnel itself isn't answering, so everything shown is last-known. */
export function SeatCard({ view, offline }: { view: SeatView; offline: boolean }) {
  const s = useScale();
  const styles = useScaledStyles(makeStyles);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const onLayout = (e: LayoutChangeEvent) =>
    setSize({ w: e.nativeEvent.layout.width, h: e.nativeEvent.layout.height });

  const p = view.position;
  const mode: Mode = p?.position != null ? 'position' : 'travel';
  const sc = scaleFor(mode, view.history);
  const { d, last } = sc ? buildPath(view.history, mode, sc, size.w, size.h) : { d: '', last: null };

  const dev = view.raw?.dev ?? view.presence?.dev;
  const up = view.presence?.up;
  const veil = offline ? 'FUNNEL UNREACHABLE' : !p ? 'NO DATA' : p.stale ? `STALE · ${fmt((p.age_ms ?? 0) / 1000, 1, ' s')}` : null;
  const rawValues = view.raw ? Object.entries(view.raw.values) : [];

  return (
    <View style={styles.card}>
      <View style={styles.header}>
        <Text style={styles.seat} numberOfLines={1}>
          SEAT {view.seat}
        </Text>
        {dev && (
          <Text style={styles.dev} numberOfLines={1}>
            {dev}
          </Text>
        )}
        <View style={{ flex: 1 }} />
        {up !== undefined && <Badge label={up ? 'UP' : 'DOWN'} tone={up ? 'good' : 'alert'} />}
        {p && !p.calibrated && <Badge label="UNCALIBRATED" tone="plain" />}
      </View>

      <View style={styles.plotRow}>
        <View style={styles.axis}>
          <Text style={styles.axisText}>{mode === 'position' ? 'FINISH' : sc ? `${Math.round(sc.hi)}` : ''}</Text>
          <Text style={styles.axisUnit}>{mode === 'position' ? '' : 'mm'}</Text>
          <Text style={styles.axisText}>{mode === 'position' ? 'CATCH' : sc ? `${Math.round(sc.lo)}` : ''}</Text>
        </View>
        <View style={styles.plot} onLayout={onLayout}>
          <View style={[styles.guide, { top: 0 }]} />
          <View style={[styles.guide, { bottom: 0 }]} />
          <Svg style={StyleSheet.absoluteFill}>
            {d !== '' && <Path d={d} fill="none" stroke={colors.ink} strokeWidth={2.5} strokeLinejoin="round" />}
            {last && <Circle cx={last.x} cy={last.y} r={5 * s} fill={colors.ink} />}
          </Svg>
          {mode === 'travel' && (
            <Text style={styles.plotNote}>raw sensor distance · autoscaled for display</Text>
          )}
        </View>
      </View>

      <View style={styles.readouts}>
        <Readout label="POSITION" value={fmt(p?.position, 3)} />
        <Readout label="TRAVEL" value={fmt(p?.travel_mm, 0, 'mm')} />
        <Readout label="VEL mm/s" value={fmt(p?.velocity_mms, 0)} />
        <Readout label="AGE" value={fmt(p?.age_ms, 0, 'ms')} />
        <Readout label="RATE" value={fmt(view.raw?.rate_hz, 1, 'Hz')} />
        <Readout label="GAPS" value={fmt(view.stats?.n_gap)} />
      </View>
      <Text style={styles.raw} numberOfLines={1}>
        {rawValues.length
          ? `raw ${view.raw?.seq != null ? `#${view.raw.seq}  ` : ''}${rawValues.map(([k, v]) => `${k}=${String(v)}`).join('  ')}`
          : `streams: ${view.streams.join(', ') || 'none'}`}
      </Text>

      {veil && (
        <View style={styles.veil}>
          <Text style={styles.veilText}>{veil}</Text>
        </View>
      )}
    </View>
  );
}

function Readout({ label, value }: { label: string; value: string }) {
  const styles = useScaledStyles(makeStyles);
  return (
    <View style={styles.readout}>
      <Text style={styles.readoutLabel}>{label}</Text>
      <Text style={styles.readoutValue} numberOfLines={1}>
        {value}
      </Text>
    </View>
  );
}

function Badge({ label, tone }: { label: string; tone: 'good' | 'alert' | 'plain' }) {
  const styles = useScaledStyles(makeStyles);
  const bg = tone === 'good' ? colors.goodBg : tone === 'alert' ? colors.alert : colors.zone;
  const fg = tone === 'good' ? colors.goodFg : tone === 'alert' ? colors.onAlert : colors.muted;
  return (
    <View style={[styles.badge, { backgroundColor: bg }]}>
      <Text style={[styles.badgeText, { color: fg }]}>{label}</Text>
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    card: {
      height: 176 * s,
      borderRadius: radii.card * s,
      backgroundColor: colors.subtle,
      paddingHorizontal: 14 * s,
      paddingTop: 10 * s,
      paddingBottom: 8 * s,
      gap: 6 * s,
      overflow: 'hidden',
    },
    header: { flexDirection: 'row', alignItems: 'center', gap: 8 * s },
    seat: { flexShrink: 0, fontFamily: fonts.extrabold, fontSize: 15 * s, letterSpacing: 0.6 * s, color: colors.ink },
    dev: { flexShrink: 1, fontFamily: fonts.mono, fontSize: 11 * s, color: colors.muted },
    badge: { height: 18 * s, paddingHorizontal: 6 * s, borderRadius: 5 * s, justifyContent: 'center' },
    badgeText: { fontFamily: fonts.bold, fontSize: 9.5 * s, letterSpacing: 0.8 * s },
    plotRow: { flex: 1, flexDirection: 'row', gap: 8 * s },
    axis: { width: 40 * s, justifyContent: 'space-between' },
    axisText: { fontFamily: fonts.bold, fontSize: 9.5 * s, letterSpacing: 0.6 * s, color: colors.muted, ...tabular },
    axisUnit: { fontFamily: fonts.mono, fontSize: 9 * s, color: colors.guide },
    plot: { flex: 1 },
    guide: { position: 'absolute', left: 0, right: 0, height: 1, backgroundColor: colors.track },
    plotNote: {
      position: 'absolute',
      left: 0,
      bottom: 3 * s,
      fontFamily: fonts.mono,
      fontSize: 9 * s,
      color: colors.guide,
    },
    readouts: { flexDirection: 'row', gap: 6 * s },
    readout: { flex: 1, minWidth: 0 },
    readoutLabel: { fontFamily: fonts.bold, fontSize: 9 * s, letterSpacing: 0.8 * s, color: colors.muted },
    readoutValue: { fontFamily: fonts.monoSemibold, fontSize: 11 * s, color: colors.ink, ...tabular },
    raw: { fontFamily: fonts.mono, fontSize: 10 * s, color: colors.muted },
    veil: {
      position: 'absolute',
      top: 0,
      right: 0,
      bottom: 0,
      left: 0,
      backgroundColor: colors.noSignalVeil,
      alignItems: 'center',
      justifyContent: 'center',
    },
    veilText: {
      backgroundColor: colors.alert,
      color: colors.onAlert,
      borderRadius: 10 * s,
      overflow: 'hidden',
      paddingVertical: 8 * s,
      paddingHorizontal: 14 * s,
      fontFamily: fonts.extrabold,
      fontSize: 14 * s,
      letterSpacing: 0.8 * s,
    },
  });
