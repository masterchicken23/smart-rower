import { router } from 'expo-router';
import { useEffect, useState, type ReactNode } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';
import Svg, { Circle, Line, Rect } from 'react-native-svg';

import { FunnelPill } from '@/components/FunnelPill';
import { APRILTAG_TRAIL_MS, APRILTAG_TRAIL_OPACITY } from '@/config';
import { useFunnel, useSlice } from '@/data/RowingProvider';
import { fmt, type Connection, type SeatView } from '@/data/funnel/store';
import { isAprilTagRecord, type AprilTag, type AprilTagRecord } from '@/data/funnel/types';
import { useScale, useScaledStyles, useScreenPadding } from '@/theme/scale';
import { colors, fonts, radii, tabular } from '@/theme/tokens';

/** Frame shape to draw before the first record says the real one. */
const PLACEHOLDER_FRAME = { w: 16, h: 9 };

/** Where one tag was in recent records, oldest first, in image pixels. `null` marks a record it was missing from. */
type TrailPoint = { t: number; x: number; y: number } | null;
type Trails = { tick: number | null; byTag: Map<number, TrailPoint[]> };

/** Adds a record's tags to the trails and forgets points older than the trail lasts. */
function extendTrails(prev: Trails, rec: AprilTagRecord): Trails {
  const cutoff = rec.t - APRILTAG_TRAIL_MS / 1000;
  const byTag = new Map<number, TrailPoint[]>();
  const ids = new Set([...prev.byTag.keys(), ...rec.tags.map((tag) => tag.id)]);
  for (const id of ids) {
    const tag = rec.tags.find((x) => x.id === id);
    const next = [...(prev.byTag.get(id) ?? []), tag ? { t: rec.t, x: tag.center_px[0], y: tag.center_px[1] } : null];
    while (next.length && (next[0] === null || next[0].t < cutoff)) next.shift();
    if (next.some((p) => p !== null)) byTag.set(id, next);
  }
  return { tick: rec.tick, byTag };
}

/**
 * Each seat camera's AprilTag detections, plotted on its image plane. Points are the
 * detector's `center_px` exactly as sent: nothing is filtered or smoothed. The fading
 * trail behind each point only joins those same raw positions, and is drawn on screen only.
 */
export default function AprilTagScreen() {
  const funnel = useFunnel();
  const url = useSlice(funnel.url);
  const connection = useSlice(funnel.connection);
  const seats = useSlice(funnel.seats);
  const styles = useScaledStyles(makeStyles);
  const padding = useScreenPadding(18, 36, 20);
  const [picked, setPicked] = useState<number | null>(null);

  useEffect(() => funnel.retain(), [funnel]);

  const cameras = seats.filter((v) => v.apriltag !== null);
  const view = cameras.find((v) => v.seat === picked) ?? cameras[0] ?? null;
  const goBack = () => (router.canGoBack() ? router.back() : router.replace('/developer'));

  return (
    <View style={[styles.screen, padding]}>
      <View style={styles.topBar}>
        <Pressable
          accessibilityRole="button"
          onPress={goBack}
          style={({ pressed }) => [styles.chip, pressed && { backgroundColor: colors.border }]}
        >
          <Text style={styles.chipText}>← Developer</Text>
        </Pressable>
        <Text style={styles.title}>APRILTAG</Text>
        {cameras.map((v) => {
          const on = v.seat === view?.seat;
          return (
            <Pressable
              key={v.seat}
              accessibilityRole="button"
              accessibilityState={{ selected: on }}
              onPress={() => setPicked(v.seat)}
              style={[styles.chip, on && { backgroundColor: colors.ink }]}
            >
              <Text style={[styles.chipText, on && { color: colors.surface }]}>SEAT {v.seat}</Text>
            </Pressable>
          );
        })}
        <FunnelPill url={url} connection={connection} />
      </View>

      {view ? (
        <CameraView key={view.seat} view={view} offline={connection.state === 'unreachable'} />
      ) : (
        <EmptyState url={url} connection={connection} />
      )}
    </View>
  );
}

function CameraView({ view, offline }: { view: SeatView; offline: boolean }) {
  const s = useScale();
  const styles = useScaledStyles(makeStyles);
  const [area, setArea] = useState({ w: 0, h: 0 });
  const onLayout = (e: LayoutChangeEvent) => setArea({ w: e.nativeEvent.layout.width, h: e.nativeEvent.layout.height });

  const block = view.apriltag!;
  const rec = isAprilTagRecord(block.values) ? block.values : null;
  const frame = rec ? { w: rec.w, h: rec.h } : PLACEHOLDER_FRAME;
  // The largest box with the camera's aspect ratio that fits, so pixels stay square.
  const k = Math.min(area.w / frame.w, area.h / frame.h) || 0;
  const fw = frame.w * k;
  const fh = frame.h * k;

  const veil = offline
    ? 'FUNNEL UNREACHABLE'
    : block.stale
      ? `STALE · ${fmt((block.age_ms ?? 0) / 1000, 1, ' s')}`
      : !rec
        ? 'UNRECOGNISED RECORD'
        : null;
  const tags = rec?.tags ?? [];

  // Updated while rendering, once per new record, so the trail never lags the point.
  const [trails, setTrails] = useState<Trails>({ tick: null, byTag: new Map() });
  if (rec && rec.tick !== trails.tick) setTrails(extendTrails(trails, rec));
  const segments: { key: string; x1: number; y1: number; x2: number; y2: number; fade: number }[] = [];
  if (rec) {
    for (const [id, points] of trails.byTag) {
      for (let i = 1; i < points.length; i++) {
        const a = points[i - 1];
        const b = points[i];
        if (!a || !b) continue; // the tag left the view: a gap, not a line across it
        const fade = 1 - (rec.t - (a.t + b.t) / 2) / (APRILTAG_TRAIL_MS / 1000);
        if (fade <= 0) continue;
        segments.push({ key: `${id}-${b.t}`, x1: a.x * k, y1: a.y * k, x2: b.x * k, y2: b.y * k, fade });
      }
    }
  }

  return (
    <View style={styles.body}>
      <View style={styles.plotArea} onLayout={onLayout}>
        <View style={{ width: fw, height: fh }}>
          <Svg width={fw} height={fh}>
            <Rect x={0} y={0} width={fw} height={fh} fill={colors.subtle} stroke={colors.track} strokeWidth={1} />
            <Line x1={fw / 2} y1={0} x2={fw / 2} y2={fh} stroke={colors.track} strokeWidth={1} strokeDasharray="4 4" />
            <Line x1={0} y1={fh / 2} x2={fw} y2={fh / 2} stroke={colors.track} strokeWidth={1} strokeDasharray="4 4" />
            {segments.map((g) => (
              <Line
                key={g.key}
                x1={g.x1}
                y1={g.y1}
                x2={g.x2}
                y2={g.y2}
                stroke={colors.ink}
                strokeOpacity={APRILTAG_TRAIL_OPACITY * g.fade}
                strokeWidth={(1 + 2.5 * g.fade) * s}
                strokeLinecap="round"
              />
            ))}
            {tags.map((tag, i) => (
              <Circle key={i} cx={tag.center_px[0] * k} cy={tag.center_px[1] * k} r={7 * s} fill={colors.ink} />
            ))}
          </Svg>
          {tags.map((tag, i) => (
            <Text
              key={i}
              style={[styles.tagLabel, { left: tag.center_px[0] * k + 10 * s, top: tag.center_px[1] * k - 20 * s }]}
            >
              {`#${tag.id}`}
            </Text>
          ))}
          <Text style={[styles.corner, { top: 4 * s, left: 6 * s }]}>0,0</Text>
          <Text style={[styles.corner, { bottom: 4 * s, right: 6 * s }]}>
            {rec ? `${rec.w}×${rec.h} px` : ''}
          </Text>
          {rec && rec.n === 0 && !veil && <Text style={styles.noTag}>NO TAG IN VIEW</Text>}
          {veil && (
            <View style={styles.veil}>
              <Text style={styles.veilText}>{veil}</Text>
            </View>
          )}
        </View>
      </View>

      <ScrollView style={styles.panel} contentContainerStyle={styles.panelContent} showsVerticalScrollIndicator={false}>
        <Section title={`CAMERA ${rec?.cam ?? block.dev ?? ''}`.trim()}>
          <Readout label="RATE" value={fmt(block.rate_hz, 1, ' Hz')} />
          <Readout label="AGE" value={fmt(block.age_ms, 0, ' ms')} />
          <Readout label="E2E" value={fmt(rec?.ms.e2e, 1, ' ms')} />
          <Readout label="GAPS" value={fmt(block.n_gap)} />
          <Readout label="SKIPPED" value={fmt(rec?.skipped)} />
          <Readout label="TAGS" value={fmt(rec?.n)} />
        </Section>
        {tags.map((tag, i) => (
          <TagSection key={i} tag={tag} />
        ))}
        <Text style={styles.note}>raw center_px · camera frame +x right, +y down</Text>
      </ScrollView>
    </View>
  );
}

function TagSection({ tag }: { tag: AprilTag }) {
  const [x, y, z] = tag.pos_m;
  const [rx, ry, rz] = tag.euler_deg;
  return (
    <Section title={`TAG #${tag.id} · ${tag.fam}`}>
      <Readout label="CENTER px" value={`${fmt(tag.center_px[0], 1)}, ${fmt(tag.center_px[1], 1)}`} />
      <Readout label="POS m" value={`${fmt(x, 3)}, ${fmt(y, 3)}, ${fmt(z, 3)}`} />
      <Readout label="DIST" value={fmt(tag.dist_m, 3, ' m')} />
      <Readout label="EULER °" value={`${fmt(rx, 1)}, ${fmt(ry, 1)}, ${fmt(rz, 1)}`} />
      <Readout label="AMBIGUITY" value={fmt(tag.ambiguity, 3)} />
      <Readout label="ERR" value={fmt(tag.err_px, 2, ' px')} />
      <Readout label="MARGIN" value={fmt(tag.margin, 1)} />
      <Readout label="HAMMING" value={fmt(tag.hamming)} />
    </Section>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  const styles = useScaledStyles(makeStyles);
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle} numberOfLines={1}>
        {title}
      </Text>
      {children}
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

function EmptyState({ url, connection }: { url: string; connection: Connection }) {
  const styles = useScaledStyles(makeStyles);
  let title: string;
  let body: string;
  switch (connection.state) {
    case 'unreachable':
      title = "Can't reach the funnel";
      body = `${url} didn't answer (${connection.error ?? 'no response'}). Change the address on the Developer screen.`;
      break;
    case 'ok':
    case 'stale-tick':
    case 'starting':
      title = 'No cameras yet';
      body =
        'No seat has an apriltag stream. A camera appears here once its detector publishes and the funnel maps it ' +
        'to a seat (FUNNEL_APRILTAG_CAMERAS=cam1:1,…).';
      break;
    default:
      title = 'Connecting…';
      body = `Polling ${url}/snapshot.`;
  }
  return (
    <View style={styles.empty}>
      <Text style={styles.emptyTitle}>{title}</Text>
      <Text style={styles.emptyBody}>{body}</Text>
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    screen: { flex: 1, backgroundColor: colors.surface, gap: 12 * s },
    topBar: { height: 40 * s, flexDirection: 'row', alignItems: 'center', gap: 8 * s },
    chip: {
      height: 40 * s,
      paddingHorizontal: 14 * s,
      borderRadius: radii.pill * s,
      backgroundColor: colors.neutral,
      justifyContent: 'center',
    },
    chipText: { fontFamily: fonts.bold, fontSize: 15 * s, color: colors.ink },
    title: {
      flex: 1,
      marginLeft: 4 * s,
      fontFamily: fonts.extrabold,
      fontSize: 20 * s,
      letterSpacing: 0.8 * s,
      color: colors.ink,
    },
    body: { flex: 1, flexDirection: 'row', gap: 14 * s },
    plotArea: { flex: 1, alignItems: 'center', justifyContent: 'center' },
    tagLabel: { position: 'absolute', fontFamily: fonts.monoSemibold, fontSize: 12 * s, color: colors.ink },
    corner: { position: 'absolute', fontFamily: fonts.mono, fontSize: 10 * s, color: colors.guide },
    noTag: {
      position: 'absolute',
      alignSelf: 'center',
      top: '45%',
      fontFamily: fonts.extrabold,
      fontSize: 14 * s,
      letterSpacing: 0.8 * s,
      color: colors.muted,
    },
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
    panel: { width: 230 * s, flexGrow: 0 },
    panelContent: { gap: 10 * s, paddingBottom: 4 * s },
    section: { borderRadius: radii.card * s, backgroundColor: colors.subtle, padding: 12 * s, gap: 4 * s },
    sectionTitle: { fontFamily: fonts.extrabold, fontSize: 13 * s, letterSpacing: 0.6 * s, color: colors.ink },
    readout: { flexDirection: 'row', justifyContent: 'space-between', gap: 8 * s },
    readoutLabel: { fontFamily: fonts.bold, fontSize: 10 * s, letterSpacing: 0.8 * s, color: colors.muted },
    readoutValue: { flexShrink: 1, fontFamily: fonts.monoSemibold, fontSize: 11 * s, color: colors.ink, ...tabular },
    note: { fontFamily: fonts.mono, fontSize: 9.5 * s, color: colors.guide },
    empty: {
      flex: 1,
      borderRadius: radii.card * s,
      backgroundColor: colors.subtle,
      padding: 18 * s,
      justifyContent: 'center',
      gap: 6 * s,
    },
    emptyTitle: { fontFamily: fonts.extrabold, fontSize: 17 * s, color: colors.ink },
    emptyBody: { fontFamily: fonts.medium, fontSize: 13 * s, lineHeight: 18 * s, color: colors.muted },
  });
