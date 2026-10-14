import { StyleSheet, Text, View } from 'react-native';

import type { HandState, SeatSignal, SyncFeedback } from '@/data/types';
import { useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

type Tone = 'good' | 'alert' | 'muted';
type Content = { main: string; sub: string; tone: Tone };

const TONES: Record<Tone, { bg: string; fg: string }> = {
  good: { bg: colors.goodBg, fg: colors.goodFg },
  alert: { bg: colors.alert, fg: colors.onAlert },
  muted: { bg: colors.neutral, fg: colors.muted },
};

type BoxProps = Content & {
  /** Read by screen readers; the column header labels the box visually. */
  label: string;
  /** Portrait boxes are a little tighter horizontally. */
  compact?: boolean;
};

export function FeedbackBox({ label, main, sub, tone, compact }: BoxProps) {
  const styles = useScaledStyles(makeStyles);
  const { bg, fg } = TONES[tone];
  return (
    <View
      style={[styles.box, compact && styles.compact, { backgroundColor: bg }]}
      accessibilityLabel={`${label}: ${main}. ${sub}`}
    >
      <Text style={[styles.main, { color: fg }]} numberOfLines={1} adjustsFontSizeToFit>
        {main}
      </Text>
      <Text style={[styles.sub, { color: fg }]} numberOfLines={1}>
        {sub}
      </Text>
    </View>
  );
}

const HANDS: Record<HandState, Content> = {
  good: { main: 'GOOD', sub: 'Level at finish', tone: 'good' },
  raise: { main: 'RAISE HANDS', sub: 'Low at finish', tone: 'alert' },
  lower: { main: 'LOWER HANDS', sub: 'High at finish', tone: 'alert' },
};

/** The Jetson has no hand or oar-height feed yet, so a null state says so rather than guessing. */
const NO_HANDS: Content = { main: 'NO DATA', sub: 'No hand sensor yet', tone: 'muted' };

export function HandsBox({ state, compact }: { state: HandState | null; compact?: boolean }) {
  return <FeedbackBox label="Hands" compact={compact} {...(state ? HANDS[state] : NO_HANDS)} />;
}

type SyncProps = {
  sync: SyncFeedback | null;
  isStroke: boolean;
  signal: SeatSignal;
  /** Stroke's signal: without it there is nothing to measure against. */
  reference: SeatSignal;
  compact?: boolean;
};

function syncContent({ sync, isStroke, signal, reference }: SyncProps): Content {
  if (isStroke) return { main: 'REFERENCE', sub: 'Crew syncs to stroke', tone: 'muted' };
  if (signal === 'offline' || reference === 'offline') return { main: 'NO SIGNAL', sub: 'Jetson unreachable', tone: 'muted' };
  if (signal === 'calibrating') return { main: 'CALIBRATING', sub: 'Row one full stroke', tone: 'muted' };
  if (signal === 'no-signal') return { main: 'NO SIGNAL', sub: 'No seat reading', tone: 'muted' };
  if (reference !== 'live') return { main: 'NO REFERENCE', sub: 'Stroke seat silent', tone: 'muted' };
  if (!sync) return { main: 'WAITING', sub: 'Timing first stroke', tone: 'muted' };

  const sign = sync.ms > 0 ? '+' : sync.ms < 0 ? '−' : '±';
  const sub = `${sign}${Math.abs(sync.ms)} ms vs stroke`;
  if (sync.kind === 'in') return { main: 'IN SYNC', sub, tone: 'good' };
  return { main: `${sync.kind === 'late' ? 'LATE' : 'EARLY'} ${sync.point}`, sub, tone: 'alert' };
}

export function SyncBox(props: SyncProps) {
  return <FeedbackBox label="Sync" compact={props.compact} {...syncContent(props)} />;
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    box: {
      flex: 1,
      borderRadius: radii.control * s,
      paddingVertical: 8 * s,
      paddingHorizontal: 12 * s,
      justifyContent: 'center',
      gap: 3 * s,
      overflow: 'hidden',
    },
    compact: { paddingHorizontal: 11 * s },
    main: { fontFamily: fonts.extrabold, fontSize: 18 * s, lineHeight: 18 * s },
    sub: { fontFamily: fonts.semibold, fontSize: 11 * s },
  });
