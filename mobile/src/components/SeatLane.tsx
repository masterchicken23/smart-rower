import { useEffect } from 'react';
import { StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';
import Animated, { useAnimatedStyle, useSharedValue, type SharedValue } from 'react-native-reanimated';

import { RAIL_GAP_TOLERANCE } from '@/config';
import type { CatchSide, SeatSignal } from '@/data/types';
import { useScale, useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

const ZONE_FRACTION = 0.08;

const SIZES = {
  // Landscape: the lane fills its row; the dot carries the seat letter.
  regular: { inset: 18, dot: 28, ghostHeight: 24, radius: radii.lane },
  // Portrait: a fixed-height lane beside the seat letter.
  compact: { inset: 14, dot: 24, ghostHeight: 22, radius: radii.laneCompact },
} as const;

const SIGNAL_TEXT: Record<Exclude<SeatSignal, 'live'>, string> = {
  calibrating: 'CALIBRATING',
  'no-signal': 'NO SIGNAL',
  offline: 'NO CONNECTION',
};

type Props = {
  /** This seat's normalized position, 0 catch … 1 finish. */
  value: SharedValue<number>;
  /** Stroke's position, ghosted behind every other seat. Null on stroke's own lane. */
  reference: SharedValue<number> | null;
  signal: SeatSignal;
  /** Whether stroke has a live position, so the ghost and gap bar mean something. */
  referenceLive: boolean;
  catchSide: CatchSide;
  /** Letter inside the dot; omitted in portrait, where it sits beside the lane. */
  letter?: string;
  compact?: boolean;
};

/** One seat's slide: catch and finish end zones, the rail, and the seat's dot. */
export function SeatLane({ value, reference, signal, referenceLive, catchSide, letter, compact }: Props) {
  const s = useScale();
  const styles = useScaledStyles(makeStyles);
  const size = SIZES[compact ? 'compact' : 'regular'];
  const dot = size.dot * s;
  const width = useSharedValue(0);
  const catchLeft = useSharedValue(catchSide === 'Left');

  useEffect(() => {
    catchLeft.value = catchSide === 'Left';
  }, [catchSide, catchLeft]);

  const onLayout = (e: LayoutChangeEvent) => {
    width.value = e.nativeEvent.layout.width;
  };

  const dotStyle = useAnimatedStyle(() => {
    const v = catchLeft.value ? value.value : 1 - value.value;
    return { transform: [{ translateX: v * width.value - dot / 2 }] };
  });

  const ghostStyle = useAnimatedStyle(() => {
    if (!reference) return {};
    const v = catchLeft.value ? reference.value : 1 - reference.value;
    return { transform: [{ translateX: v * width.value - 2 * s }] };
  });

  const gapStyle = useAnimatedStyle(() => {
    if (!reference) return {};
    const a = (catchLeft.value ? reference.value : 1 - reference.value) * width.value;
    const b = (catchLeft.value ? value.value : 1 - value.value) * width.value;
    return {
      left: Math.min(a, b),
      width: Math.abs(a - b),
      backgroundColor:
        Math.abs(reference.value - value.value) <= RAIL_GAP_TOLERANCE ? colors.goodFg : colors.alert,
    };
  });

  const live = signal === 'live';
  const isStroke = reference === null;
  const showReference = !isStroke && live && referenceLive;

  return (
    <View
      style={[styles.lane, compact && styles.compactLane, { borderRadius: size.radius * s }]}
      accessibilityLabel={live ? undefined : SIGNAL_TEXT[signal]}
    >
      <View style={[styles.zone, { left: 0 }]} />
      <View style={[styles.zone, { right: 0 }]} />
      <View style={[styles.track, { left: size.inset * s, right: size.inset * s }]} onLayout={onLayout}>
        <View style={styles.rail} />
        {showReference && (
          <>
            <Animated.View style={[styles.gap, gapStyle]} />
            <Animated.View
              style={[
                styles.ghost,
                { height: size.ghostHeight * s, marginTop: (-size.ghostHeight / 2) * s },
                ghostStyle,
              ]}
            />
          </>
        )}
        {live && (
          <Animated.View
            style={[
              styles.dot,
              isStroke ? styles.strokeDot : styles.seatDot,
              { width: dot, height: dot, marginTop: -dot / 2, borderRadius: dot / 2 },
              dotStyle,
            ]}
          >
            {letter && (
              <Text style={[styles.dotText, { color: isStroke ? colors.surface : colors.ink }]}>{letter}</Text>
            )}
          </Animated.View>
        )}
      </View>
      {!live && (
        <View style={styles.veil} pointerEvents="none">
          <Text style={styles.veilText}>{SIGNAL_TEXT[signal]}</Text>
        </View>
      )}
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    lane: { flex: 1, backgroundColor: colors.subtle, overflow: 'hidden' },
    compactLane: { height: 36 * s },
    zone: {
      position: 'absolute',
      top: 0,
      bottom: 0,
      width: `${ZONE_FRACTION * 100}%`,
      backgroundColor: colors.zone,
    },
    track: { position: 'absolute', top: 0, bottom: 0 },
    rail: {
      position: 'absolute',
      left: 0,
      right: 0,
      top: '50%',
      height: 6 * s,
      marginTop: -3 * s,
      borderRadius: 3 * s,
      backgroundColor: colors.track,
    },
    gap: {
      position: 'absolute',
      top: '50%',
      height: 6 * s,
      marginTop: -3 * s,
      borderRadius: 3 * s,
    },
    ghost: {
      position: 'absolute',
      left: 0,
      top: '50%',
      width: 4 * s,
      borderRadius: 2 * s,
      backgroundColor: colors.guide,
    },
    dot: {
      position: 'absolute',
      left: 0,
      top: '50%',
      alignItems: 'center',
      justifyContent: 'center',
    },
    strokeDot: { backgroundColor: colors.ink },
    seatDot: { backgroundColor: colors.surface, borderWidth: 3 * s, borderColor: colors.ink },
    dotText: { fontFamily: fonts.extrabold, fontSize: 12 * s },
    veil: { ...StyleSheet.absoluteFill, alignItems: 'center', justifyContent: 'center' },
    veilText: { fontFamily: fonts.bold, fontSize: 11 * s, letterSpacing: 1.5 * s, color: colors.muted },
  });
