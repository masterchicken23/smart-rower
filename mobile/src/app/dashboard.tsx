import { useKeepAwake } from 'expo-keep-awake';
import { useEffect, useState } from 'react';
import { Pressable, StyleSheet, Text, View, useWindowDimensions, type TextStyle, type ViewStyle } from 'react-native';

import { HandsBox, SyncBox } from '@/components/FeedbackBox';
import { SeatLane } from '@/components/SeatLane';
import { SessionControls } from '@/components/SessionControls';
import { useRowing, useSlice } from '@/data/RowingProvider';
import { elapsedMs, type RowingStore } from '@/data/store';
import type { CatchSide, Seat, SeatFeedback, SeatSignal, SessionState } from '@/data/types';
import { useScaledStyles, useScreenPadding } from '@/theme/scale';
import { colors, fonts, tabular } from '@/theme/tokens';

type ViewProps = {
  store: RowingStore;
  session: SessionState;
  crew: Seat[];
  signals: Record<number, SeatSignal>;
  feedback: Record<number, SeatFeedback>;
  catchSide: CatchSide;
  rate: string;
};

/** Live crew feedback for the coxswain, from the Jetson's funnel. Works in either orientation. */
export default function Dashboard() {
  useKeepAwake();
  const store = useRowing();
  const session = useSlice(store.session);
  const crew = useSlice(store.crew);
  const signals = useSlice(store.signal);
  const feedback = useSlice(store.feedback);
  const catchSide = useSlice(store.catchSide);
  const { width, height } = useWindowDimensions();

  useEffect(() => store.attach(), [store]);
  useEffect(() => {
    store.beginSession();
  }, [store]);

  // Rate comes from stroke's strokes, so it isn't current while stroke has no signal.
  const strokeLive = signals[crew[0].seat] === 'live';
  const rate = strokeLive && session.rateSpm !== null ? String(session.rateSpm) : '--';

  const props: ViewProps = { store, session, crew, signals, feedback, catchSide, rate };
  return width > height ? <Landscape {...props} /> : <Portrait {...props} />;
}

function Landscape({ store, session, crew, signals, feedback, catchSide, rate }: ViewProps) {
  const styles = useScaledStyles(makeLandscape);
  const padding = useScreenPadding(16, 36, 20);
  const strokeSeat = crew[0].seat;
  const referenceSignal = signals[strokeSeat] ?? 'no-signal';

  return (
    <View style={[styles.screen, padding]}>
      <View style={styles.topBar}>
        <View style={styles.elapsed}>
          <Text style={styles.metricLabel}>ELAPSED</Text>
          <Elapsed session={session} style={styles.elapsedValue} />
        </View>
        <View style={styles.rate}>
          <Text style={styles.rateValue}>{rate}</Text>
          <Text style={styles.rateUnit}>SPM</Text>
        </View>
        <View style={styles.controls}>
          <SessionControls
            layout="bar"
            running={session.running}
            onTogglePause={() => store.togglePause()}
            onReset={() => store.reset()}
          />
        </View>
      </View>

      <View style={styles.body}>
        <View style={styles.headerRow}>
          <View style={styles.labelColumn} />
          <CatchFinishLabels store={store} catchSide={catchSide} style={styles.laneHeader} labelStyle={styles.columnLabel} />
          <Text style={[styles.columnLabel, styles.boxColumn]}>HANDS</Text>
          <Text style={[styles.columnLabel, styles.boxColumn]}>SYNC</Text>
        </View>

        {crew.map((seat) => {
          const signal = signals[seat.seat] ?? 'no-signal';
          return (
            <View key={seat.seat} style={styles.seatRow}>
              <View style={[styles.labelColumn, styles.seatLabel]}>
                <Text style={styles.seatName} numberOfLines={1}>
                  {seat.name}
                </Text>
                <Text style={styles.seatSub}>{seat.sub}</Text>
              </View>
              <SeatLane
                value={store.position(seat.seat)}
                reference={seat.isStroke ? null : store.position(strokeSeat)}
                signal={signal}
                referenceLive={referenceSignal === 'live'}
                catchSide={catchSide}
                letter={seat.short}
              />
              <View style={styles.boxColumn}>
                <HandsBox state={feedback[seat.seat]?.hands ?? null} />
              </View>
              <View style={styles.boxColumn}>
                <SyncBox
                  sync={feedback[seat.seat]?.sync ?? null}
                  isStroke={seat.isStroke}
                  signal={signal}
                  reference={referenceSignal}
                />
              </View>
            </View>
          );
        })}
      </View>
    </View>
  );
}

function Portrait({ store, session, crew, signals, feedback, catchSide, rate }: ViewProps) {
  const styles = useScaledStyles(makePortrait);
  const padding = useScreenPadding(54, 22, 26);
  const strokeSeat = crew[0].seat;
  const referenceSignal = signals[strokeSeat] ?? 'no-signal';

  return (
    <View style={[styles.screen, padding]}>
      <View style={styles.metrics}>
        <View style={styles.metric}>
          <Text style={styles.metricLabel}>SPM</Text>
          <Text style={styles.rateValue}>{rate}</Text>
        </View>
        <View style={[styles.metric, { alignItems: 'flex-end' }]}>
          <Text style={styles.metricLabel}>ELAPSED</Text>
          <Elapsed session={session} style={styles.elapsedValue} />
        </View>
      </View>

      <View style={styles.lanes}>
        <CatchFinishLabels store={store} catchSide={catchSide} style={styles.laneHeader} labelStyle={styles.columnLabel} />
        {crew.map((seat) => (
          <View key={seat.seat} style={styles.laneRow}>
            <Text style={styles.laneLetter}>{seat.short}</Text>
            <SeatLane
              compact
              value={store.position(seat.seat)}
              reference={seat.isStroke ? null : store.position(strokeSeat)}
              signal={signals[seat.seat] ?? 'no-signal'}
              referenceLive={referenceSignal === 'live'}
              catchSide={catchSide}
            />
          </View>
        ))}
      </View>

      <View style={styles.grid}>
        <View style={styles.headerRow}>
          <View style={styles.labelColumn} />
          <Text style={[styles.columnLabel, styles.boxColumn]}>HANDS</Text>
          <Text style={[styles.columnLabel, styles.boxColumn]}>SYNC</Text>
        </View>
        {crew.map((seat) => (
          <View key={seat.seat} style={styles.seatRow}>
            <View style={[styles.labelColumn, styles.seatLabel]}>
              <Text style={styles.seatName} numberOfLines={1}>
                {seat.name}
              </Text>
              <Text style={styles.seatSub}>{seat.sub}</Text>
            </View>
            <View style={styles.boxColumn}>
              <HandsBox compact state={feedback[seat.seat]?.hands ?? null} />
            </View>
            <View style={styles.boxColumn}>
              <SyncBox
                compact
                sync={feedback[seat.seat]?.sync ?? null}
                isStroke={seat.isStroke}
                signal={signals[seat.seat] ?? 'no-signal'}
                reference={referenceSignal}
              />
            </View>
          </View>
        ))}
      </View>

      <SessionControls
        layout="footer"
        running={session.running}
        onTogglePause={() => store.togglePause()}
        onReset={() => store.reset()}
      />
    </View>
  );
}

type LabelProps = { store: RowingStore; catchSide: CatchSide; style: ViewStyle; labelStyle: TextStyle };

/** Tapping the labels swaps which end of the lanes is the catch. */
function CatchFinishLabels({ store, catchSide, style, labelStyle }: LabelProps) {
  const [left, right] = catchSide === 'Left' ? ['CATCH', 'FINISH'] : ['FINISH', 'CATCH'];
  return (
    <Pressable
      onPress={() => store.toggleCatchSide()}
      accessibilityRole="button"
      accessibilityLabel="Swap catch side"
      style={style}
    >
      <Text style={labelStyle}>{left}</Text>
      <Text style={labelStyle}>{right}</Text>
    </Pressable>
  );
}

function Elapsed({ session, style }: { session: SessionState; style: TextStyle }) {
  const [now, setNow] = useState(Date.now);

  useEffect(() => {
    if (!session.running) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [session.running]);

  const total = Math.floor(elapsedMs(session, session.running ? now : undefined) / 1000);
  const mm = Math.floor(total / 60);
  const ss = String(total % 60).padStart(2, '0');
  return <Text style={style}>{`${mm}:${ss}`}</Text>;
}

/** Text styles both layouts share. */
const sharedStyles = (s: number) =>
  ({
    metricLabel: { fontFamily: fonts.bold, fontSize: 12 * s, letterSpacing: 1.4 * s, color: colors.muted },
    columnLabel: {
      fontFamily: fonts.bold,
      fontSize: 11 * s,
      lineHeight: 14 * s,
      letterSpacing: 1.5 * s,
      color: colors.muted,
    },
    seatSub: { fontFamily: fonts.semibold, fontSize: 12 * s, color: colors.muted },
  }) satisfies Record<string, TextStyle>;

const makeLandscape = (s: number) =>
  StyleSheet.create({
    ...sharedStyles(s),
    screen: { flex: 1, backgroundColor: colors.surface, gap: 10 * s },
    topBar: { height: 58 * s, flexDirection: 'row', alignItems: 'center' },
    elapsed: { flex: 1, gap: 2 * s },
    elapsedValue: {
      fontFamily: fonts.bold,
      fontSize: 32 * s,
      lineHeight: 32 * s,
      color: colors.ink,
      ...tabular,
    },
    rate: { flexDirection: 'row', alignItems: 'baseline', gap: 8 * s },
    rateValue: {
      fontFamily: fonts.extrabold,
      fontSize: 64 * s,
      lineHeight: 64 * s,
      letterSpacing: -1.3 * s,
      color: colors.ink,
      ...tabular,
    },
    rateUnit: { fontFamily: fonts.bold, fontSize: 16 * s, letterSpacing: 1.6 * s, color: colors.muted },
    controls: { flex: 1 },

    body: { flex: 1, gap: 6 * s },
    headerRow: { height: 14 * s, flexDirection: 'row', gap: 10 * s },
    laneHeader: { flex: 1, flexDirection: 'row', justifyContent: 'space-between', paddingHorizontal: 4 * s },
    seatRow: { flex: 1, flexDirection: 'row', gap: 10 * s },
    labelColumn: { width: 78 * s },
    boxColumn: { width: 146 * s },
    seatLabel: { justifyContent: 'center', gap: 1 * s },
    seatName: { fontFamily: fonts.extrabold, fontSize: 16 * s, letterSpacing: 0.6 * s, color: colors.ink },
  });

const makePortrait = (s: number) =>
  StyleSheet.create({
    ...sharedStyles(s),
    screen: { flex: 1, backgroundColor: colors.surface, gap: 18 * s },
    metrics: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'flex-end' },
    metric: { gap: 2 * s },
    rateValue: {
      fontFamily: fonts.extrabold,
      fontSize: 88 * s,
      lineHeight: 80 * s,
      letterSpacing: -1.8 * s,
      color: colors.ink,
      ...tabular,
    },
    elapsedValue: {
      fontFamily: fonts.bold,
      fontSize: 34 * s,
      lineHeight: 34 * s,
      color: colors.ink,
      ...tabular,
    },

    lanes: { gap: 6 * s },
    laneHeader: {
      flexDirection: 'row',
      justifyContent: 'space-between',
      paddingLeft: 38 * s,
      paddingRight: 4 * s,
    },
    laneRow: { flexDirection: 'row', alignItems: 'center', gap: 8 * s },
    laneLetter: { width: 30 * s, fontFamily: fonts.extrabold, fontSize: 14 * s, color: colors.ink },

    grid: { flex: 1, gap: 8 * s },
    headerRow: { height: 14 * s, flexDirection: 'row', gap: 8 * s },
    seatRow: { flex: 1, flexDirection: 'row', gap: 8 * s },
    labelColumn: { width: 66 * s },
    boxColumn: { flex: 1 },
    seatLabel: { justifyContent: 'center', gap: 1 * s },
    seatName: { fontFamily: fonts.extrabold, fontSize: 15 * s, letterSpacing: 0.45 * s, color: colors.ink },
  });
