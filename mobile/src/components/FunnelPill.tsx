import { Pressable, StyleSheet, Text, View } from 'react-native';

import type { ConnState, Connection } from '@/data/funnel/store';
import { useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii, tabular } from '@/theme/tokens';

const LABEL: Record<ConnState, string> = {
  idle: 'IDLE',
  connecting: 'CONNECTING',
  ok: 'LIVE',
  starting: 'STARTING',
  'stale-tick': 'STALLED',
  unreachable: 'UNREACHABLE',
};

type Props = { url: string; connection: Connection; onPress?: () => void };

/** Funnel connection status. With `onPress`, pressing it opens the URL editor. */
export function FunnelPill({ url, connection, onPress }: Props) {
  const styles = useScaledStyles(makeStyles);
  const { state, rttMs } = connection;
  const good = state === 'ok';
  const bad = state === 'unreachable' || state === 'stale-tick';
  const bg = good ? colors.goodBg : bad ? colors.alert : colors.neutral;
  const fg = good ? colors.goodFg : bad ? colors.onAlert : colors.ink;
  const host = url.replace(/^https?:\/\//, '') || 'no URL';
  const detail = good && rttMs !== null ? ` · ${rttMs} ms` : '';

  return (
    <Pressable
      accessibilityRole={onPress ? 'button' : 'text'}
      accessibilityLabel={`Funnel ${host} ${LABEL[state]}${detail}${onPress ? '. Change address' : ''}`}
      onPress={onPress}
      disabled={!onPress}
      style={({ pressed }) => [styles.pill, { backgroundColor: bg, opacity: pressed ? 0.8 : 1 }]}
    >
      <View style={[styles.dot, { backgroundColor: fg }]} />
      <Text style={[styles.text, { color: fg }]} numberOfLines={1}>
        {`${LABEL[state]} · ${host}${detail}`}
      </Text>
    </Pressable>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    pill: {
      height: 40 * s,
      paddingHorizontal: 14 * s,
      borderRadius: radii.pill * s,
      flexDirection: 'row',
      alignItems: 'center',
      gap: 8 * s,
      flexShrink: 1,
    },
    dot: { width: 9 * s, height: 9 * s, borderRadius: 4.5 * s },
    text: { fontFamily: fonts.bold, fontSize: 13 * s, letterSpacing: 0.5 * s, flexShrink: 1, ...tabular },
  });
