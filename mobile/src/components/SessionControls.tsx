import { Pressable, StyleSheet, Text, View } from 'react-native';

import { useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

type Props = {
  running: boolean;
  onTogglePause: () => void;
  onReset: () => void;
  /** Landscape: compact buttons in the top bar. Portrait: a full-width bottom bar. */
  layout: 'bar' | 'footer';
};

export function SessionControls({ running, onTogglePause, onReset, layout }: Props) {
  const styles = useScaledStyles(makeStyles);
  const footer = layout === 'footer';
  const button = [styles.button, footer ? styles.footerButton : styles.barButton];
  const text = [styles.text, footer ? styles.footerText : styles.barText];

  return (
    <View style={footer ? styles.footer : styles.bar}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={running ? 'Pause session' : 'Resume session'}
        onPress={onTogglePause}
        style={({ pressed }) => [
          button,
          {
            backgroundColor: running ? (pressed ? colors.pressedInk : colors.ink) : colors.goodFg,
            opacity: !running && pressed ? 0.85 : 1,
          },
        ]}
      >
        <Text style={[text, { color: colors.surface }]}>{running ? 'PAUSE' : 'RESUME'}</Text>
      </Pressable>

      <Pressable
        accessibilityRole="button"
        accessibilityLabel="Reset session"
        onPress={onReset}
        style={({ pressed }) => [button, { backgroundColor: pressed ? colors.border : colors.neutral }]}
      >
        <Text style={[text, { color: colors.ink }]}>RESET</Text>
      </Pressable>
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    bar: { flexDirection: 'row', justifyContent: 'flex-end', gap: 8 * s },
    footer: { height: 60 * s, flexDirection: 'row', gap: 10 * s },
    button: { alignItems: 'center', justifyContent: 'center' },
    barButton: { height: 44 * s, paddingHorizontal: 16 * s, borderRadius: radii.lane * s },
    footerButton: { flex: 1, borderRadius: radii.control * s },
    text: { fontFamily: fonts.extrabold },
    barText: { fontSize: 15 * s, letterSpacing: 0.9 * s },
    footerText: { fontSize: 17 * s, letterSpacing: 1 * s },
  });
