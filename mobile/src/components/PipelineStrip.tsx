import { StyleSheet, Text, View } from 'react-native';

import type { HealthResponse } from '@/data/funnel/types';
import { useScaledStyles } from '@/theme/scale';
import { colors, fonts, radii } from '@/theme/tokens';

type Props = { health: HealthResponse | null; pipeline?: string };

/**
 * Stage status of one of the funnel's derived-value pipelines, as `/health` reports it.
 * "identity" means the stage is not implemented yet and passes data through unchanged.
 */
export function PipelineStrip({ health, pipeline = 'seat_position' }: Props) {
  const styles = useScaledStyles(makeStyles);
  const stages = health?.pipelines[pipeline];
  const entries = stages ? Object.entries(stages) : [];
  const nActive = entries.filter(([, state]) => state === 'active').length;

  return (
    <View style={styles.strip}>
      <Text style={styles.name}>
        {pipeline.replace('_', ' ').toUpperCase()}
        {stages ? `  ${nActive}/${entries.length}` : ''}
      </Text>
      {stages ? (
        entries.map(([stage, state]) => {
          const active = state === 'active';
          return (
            <View
              key={stage}
              accessibilityLabel={`${stage}: ${state}`}
              style={[styles.chip, active ? styles.active : styles.identity]}
            >
              {/* Struck through while the stage is an identity no-op. */}
              <Text
                style={[
                  styles.chipText,
                  active ? { color: colors.goodFg } : { color: colors.muted, textDecorationLine: 'line-through' },
                ]}
              >
                {stage}
              </Text>
            </View>
          );
        })
      ) : (
        <Text style={styles.none}>waiting for /health</Text>
      )}
    </View>
  );
}

const makeStyles = (s: number) =>
  StyleSheet.create({
    strip: { flexDirection: 'row', alignItems: 'center', flexWrap: 'wrap', gap: 5 * s },
    name: { fontFamily: fonts.extrabold, fontSize: 11 * s, letterSpacing: 1 * s, color: colors.ink, marginRight: 2 * s },
    chip: { height: 20 * s, paddingHorizontal: 6 * s, borderRadius: radii.terminalButton * s, justifyContent: 'center' },
    active: { backgroundColor: colors.goodBg },
    identity: { backgroundColor: colors.neutral },
    chipText: { fontFamily: fonts.mono, fontSize: 10 * s },
    none: { fontFamily: fonts.mono, fontSize: 10 * s, color: colors.muted },
  });
