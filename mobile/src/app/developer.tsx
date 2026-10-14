import { router } from 'expo-router';
import { useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';

import { FunnelPill } from '@/components/FunnelPill';
import { PipelineStrip } from '@/components/PipelineStrip';
import { SeatCard } from '@/components/SeatCard';
import { Terminal } from '@/components/Terminal';
import { useFunnel, useSlice } from '@/data/RowingProvider';
import type { Connection } from '@/data/funnel/store';
import type { LogLine } from '@/data/types';
import { useScaledStyles, useScreenPadding } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

/** Live view of the Jetson's funnel: every seat it reports, its pipeline state, and a log of real events. */
export default function Developer() {
  const funnel = useFunnel();
  const url = useSlice(funnel.url);
  const connection = useSlice(funnel.connection);
  const seats = useSlice(funnel.seats);
  const health = useSlice(funnel.health);
  const lines = useSlice(funnel.log);
  const styles = useScaledStyles(makeStyles);
  const padding = useScreenPadding(18, 36, 20);

  const [frozen, setFrozen] = useState<LogLine[] | null>(null);
  const [draftUrl, setDraftUrl] = useState<string | null>(null);

  // Poll only while this screen is open; the log is kept between visits.
  useEffect(() => funnel.retain(), [funnel]);

  const goHome = () => (router.canGoBack() ? router.back() : router.replace('/'));
  const applyUrl = () => {
    if (draftUrl !== null) funnel.setUrl(draftUrl);
    setDraftUrl(null);
  };
  const tick = connection.tick !== null ? `  ·  tick ${connection.tick}` : '';

  return (
    <View style={[styles.screen, padding]}>
      <View style={styles.topBar}>
        <Pressable
          accessibilityRole="button"
          onPress={goHome}
          style={({ pressed }) => [styles.home, pressed && { backgroundColor: colors.border }]}
        >
          <Text style={styles.homeText}>← Home</Text>
        </Pressable>
        <Text style={styles.title}>DEVELOPER</Text>
        <Pressable
          accessibilityRole="button"
          onPress={() => router.push('/apriltag')}
          style={({ pressed }) => [styles.home, pressed && { backgroundColor: colors.border }]}
        >
          <Text style={styles.homeText}>AprilTag →</Text>
        </Pressable>
        {draftUrl === null ? (
          <FunnelPill url={url} connection={connection} onPress={() => setDraftUrl(url)} />
        ) : (
          <View style={styles.urlEditor}>
            <TextInput
              value={draftUrl}
              onChangeText={setDraftUrl}
              onSubmitEditing={applyUrl}
              autoFocus
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="url"
              returnKeyType="go"
              placeholder="http://10.88.164.133:8000"
              placeholderTextColor={colors.guide}
              style={styles.urlInput}
            />
            <SmallButton label="Connect" onPress={applyUrl} primary />
            <SmallButton label="Cancel" onPress={() => setDraftUrl(null)} />
          </View>
        )}
      </View>

      <View style={styles.body}>
        <View style={styles.terminalColumn}>
          <Terminal
            label={`${url}/snapshot${tick}`}
            lines={frozen ?? lines}
            paused={frozen !== null}
            onTogglePause={() => setFrozen((f) => (f ? null : lines))}
            onClear={() => {
              funnel.clearLog();
              setFrozen((f) => (f ? [] : null));
            }}
          />
        </View>
        <View style={styles.seatColumn}>
          <PipelineStrip health={health} />
          {seats.length ? (
            <ScrollView contentContainerStyle={styles.seatList} showsVerticalScrollIndicator={false}>
              {seats.map((v) => (
                <SeatCard key={v.seat} view={v} offline={connection.state === 'unreachable'} />
              ))}
            </ScrollView>
          ) : (
            <EmptyState url={url} connection={connection} />
          )}
        </View>
      </View>
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
      body =
        `${url} didn't answer (${connection.error ?? 'no response'}). Check the phone is on the same network ` +
        'as the Jetson and the funnel is running there. Tap the status pill to change the address.';
      break;
    case 'starting':
      title = 'Funnel is starting';
      body = 'It is up but has not finished its first compute tick.';
      break;
    case 'ok':
    case 'stale-tick':
      title = 'No seats yet';
      body =
        'The funnel is live, but no sensor has published. A seat appears here as soon as anything publishes ' +
        'to rower/<seat>/seat on the Jetson’s MQTT broker.';
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

function SmallButton({ label, onPress, primary }: { label: string; onPress: () => void; primary?: boolean }) {
  const styles = useScaledStyles(makeStyles);
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      style={({ pressed }) => [
        styles.smallButton,
        { backgroundColor: primary ? colors.ink : colors.neutral, opacity: pressed ? 0.8 : 1 },
      ]}
    >
      <Text style={[styles.smallButtonText, { color: primary ? colors.surface : colors.ink }]}>{label}</Text>
    </Pressable>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    screen: { flex: 1, backgroundColor: colors.surface, gap: 12 * s },
    topBar: { height: 40 * s, flexDirection: 'row', alignItems: 'center', gap: 12 * s },
    home: {
      height: 40 * s,
      paddingHorizontal: 14 * s,
      borderRadius: radii.pill * s,
      backgroundColor: colors.neutral,
      justifyContent: 'center',
    },
    homeText: { fontFamily: fonts.bold, fontSize: 15 * s, color: colors.ink },
    title: { flex: 1, fontFamily: fonts.extrabold, fontSize: 20 * s, letterSpacing: 0.8 * s, color: colors.ink },
    urlEditor: { flexDirection: 'row', alignItems: 'center', gap: 8 * s },
    urlInput: {
      width: 260 * s,
      height: 40 * s,
      paddingHorizontal: 12 * s,
      borderRadius: radii.pill * s,
      borderWidth: 1.5,
      borderColor: colors.ink,
      fontFamily: fonts.mono,
      fontSize: 13 * s,
      color: colors.ink,
    },
    smallButton: { height: 40 * s, paddingHorizontal: 14 * s, borderRadius: radii.pill * s, justifyContent: 'center' },
    smallButtonText: { fontFamily: fonts.bold, fontSize: 14 * s },
    body: { flex: 1, flexDirection: 'row', gap: 14 * s },
    terminalColumn: { flex: 1.15 },
    seatColumn: { flex: 1, gap: 8 * s },
    seatList: { gap: 10 * s, paddingBottom: 4 * s },
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
