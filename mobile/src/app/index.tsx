import { router } from 'expo-router';
import { Pressable, StyleSheet, Text, View } from 'react-native';

import { useScaledStyles, useScreenPadding } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

export default function Landing() {
  const styles = useScaledStyles(makeStyles);
  const padding = useScreenPadding(40, 48, 40);

  return (
    <View style={[styles.screen, padding]}>
      <Text style={styles.wordmark}>{'SMART\nROWER'}</Text>
      <View style={styles.buttons}>
        <Pressable
          accessibilityRole="button"
          onPress={() => router.push('/dashboard')}
          style={({ pressed }) => [styles.button, { backgroundColor: pressed ? colors.pressedInk : colors.ink }]}
        >
          <View style={styles.buttonText}>
            <Text style={[styles.title, { color: colors.surface }]}>COXSWAIN VIEW</Text>
            <Text style={[styles.subtitle, { color: colors.surface }]}>Live feedback on the water</Text>
          </View>
          <Text style={[styles.arrow, { color: colors.surface }]}>→</Text>
        </Pressable>

        <Pressable
          accessibilityRole="button"
          onPress={() => router.push('/developer')}
          style={({ pressed }) => [styles.button, { backgroundColor: pressed ? colors.border : colors.neutral }]}
        >
          <View style={styles.buttonText}>
            <Text style={[styles.title, { color: colors.ink }]}>DEVELOPER</Text>
            <Text style={[styles.subtitle, { color: colors.muted }]}>Live sensor data from the Jetson</Text>
          </View>
          <Text style={[styles.arrow, { color: colors.ink }]}>→</Text>
        </Pressable>
      </View>
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    screen: {
      flex: 1,
      backgroundColor: colors.surface,
      flexDirection: 'row',
      alignItems: 'center',
      gap: 40 * s,
    },
    wordmark: {
      flex: 1,
      fontFamily: fonts.extrabold,
      fontSize: 92 * s,
      lineHeight: 79 * s,
      paddingTop: 8 * s,
      letterSpacing: -1.8 * s,
      color: colors.ink,
    },
    buttons: { width: 340 * s, gap: 14 * s },
    button: {
      height: 112 * s,
      borderRadius: radii.landingButton * s,
      paddingHorizontal: 26 * s,
      flexDirection: 'row',
      alignItems: 'center',
      justifyContent: 'space-between',
    },
    buttonText: { gap: 4 * s },
    title: { fontFamily: fonts.extrabold, fontSize: 26 * s, letterSpacing: 0.8 * s },
    subtitle: { fontFamily: fonts.semibold, fontSize: 15 * s },
    arrow: { fontFamily: fonts.bold, fontSize: 30 * s },
  });
