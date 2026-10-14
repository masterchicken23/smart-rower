import { useState } from 'react';
import { Pressable, StyleSheet, Text, View, type LayoutChangeEvent } from 'react-native';

import type { LogKind, LogLine } from '@/data/types';
import { useScale, useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

const LINE_HEIGHT = 14;
const LINE_GAP = 2;

const SOURCE_COLOR: Record<LogKind, string> = {
  normal: colors.term.text,
  sys: colors.term.dimmer,
  sync: colors.term.ok,
  warn: colors.term.warn,
};

const MESSAGE_COLOR: Record<LogKind, string> = {
  normal: colors.term.msg,
  sys: colors.term.msg,
  sync: colors.term.ok,
  warn: colors.term.warn,
};

type Props = {
  label: string;
  lines: LogLine[];
  paused: boolean;
  /** Data-source toggle button; omitted when the screen has only one source. */
  sourceLabel?: string;
  onToggleSource?: () => void;
  onTogglePause: () => void;
  onClear: () => void;
};

export function Terminal({ label, lines, paused, sourceLabel, onToggleSource, onTogglePause, onClear }: Props) {
  const s = useScale();
  const styles = useScaledStyles(makeStyles);
  const [visibleCount, setVisibleCount] = useState(18);

  const onLayout = (e: LayoutChangeEvent) => {
    const inner = e.nativeEvent.layout.height - 18 * s;
    setVisibleCount(Math.max(1, Math.floor((inner + LINE_GAP * s) / ((LINE_HEIGHT + LINE_GAP) * s))));
  };

  const tail = lines.slice(-visibleCount);

  return (
    <View style={styles.terminal}>
      <View style={styles.header}>
        <Text style={styles.url} numberOfLines={1}>
          {label}
        </Text>
        {sourceLabel && onToggleSource && <TermButton label={sourceLabel} onPress={onToggleSource} />}
        <TermButton label={paused ? 'resume' : 'pause'} onPress={onTogglePause} />
        <TermButton label="clear" onPress={onClear} />
      </View>
      <View style={styles.log} onLayout={onLayout}>
        {tail.map((ln) => (
          <View key={ln.id} style={styles.line}>
            <Text style={[styles.cell, { color: colors.term.dim }]}>{ln.t}</Text>
            <Text style={[styles.cell, styles.src, { color: SOURCE_COLOR[ln.kind] }]}>{ln.src}</Text>
            <Text style={[styles.cell, styles.msg, { color: MESSAGE_COLOR[ln.kind] }]} numberOfLines={1}>
              {ln.msg}
            </Text>
          </View>
        ))}
      </View>
    </View>
  );
}

function TermButton({ label, onPress }: { label: string; onPress: () => void }) {
  const styles = useScaledStyles(makeStyles);
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [styles.button, pressed && { backgroundColor: colors.term.border }]}
    >
      <Text style={styles.buttonText}>{label}</Text>
    </Pressable>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    terminal: {
      flex: 1,
      borderRadius: radii.card * s,
      backgroundColor: colors.term.bg,
      overflow: 'hidden',
    },
    header: {
      height: 34 * s,
      flexDirection: 'row',
      alignItems: 'center',
      gap: 8 * s,
      paddingLeft: 14 * s,
      paddingRight: 10 * s,
      borderBottomWidth: 1,
      borderBottomColor: colors.term.border,
    },
    url: { flex: 1, fontFamily: fonts.mono, fontSize: 11 * s, color: colors.term.dimmer },
    button: {
      height: 24 * s,
      paddingHorizontal: 10 * s,
      borderWidth: 1,
      borderColor: colors.term.buttonBorder,
      borderRadius: radii.terminalButton * s,
      justifyContent: 'center',
    },
    buttonText: { fontFamily: fonts.mono, fontSize: 11 * s, color: colors.term.text },
    log: {
      flex: 1,
      justifyContent: 'flex-end',
      paddingTop: 8 * s,
      paddingBottom: 10 * s,
      paddingHorizontal: 14 * s,
      gap: LINE_GAP * s,
      overflow: 'hidden',
    },
    line: { flexDirection: 'row', gap: 8 * s, height: LINE_HEIGHT * s },
    cell: { fontFamily: fonts.mono, fontSize: 11 * s, lineHeight: LINE_HEIGHT * s },
    src: { width: 24 * s },
    msg: { flex: 1 },
  });
