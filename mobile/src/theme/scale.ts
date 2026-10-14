import { useMemo } from 'react';
import { useWindowDimensions } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { DESIGN_HEIGHT, DESIGN_WIDTH } from './tokens';

const MIN_SCALE = 0.78;
const MAX_SCALE = 1.4;

export function useScale(): number {
  const { width, height } = useWindowDimensions();
  const long = Math.max(width, height);
  const short = Math.min(width, height);
  const s = Math.min(long / DESIGN_WIDTH, short / DESIGN_HEIGHT);
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, s));
}

/** Builds a StyleSheet from a factory that receives the proportional scale factor. */
export function useScaledStyles<T>(factory: (s: number) => T): T {
  const s = useScale();
  return useMemo(() => factory(s), [factory, s]);
}

/** Screen padding from the design, widened where the notch or home indicator needs it. */
export function useScreenPadding(top: number, horizontal: number, bottom: number) {
  const s = useScale();
  const insets = useSafeAreaInsets();
  return {
    paddingTop: Math.max(top * s, insets.top),
    paddingBottom: Math.max(bottom * s, insets.bottom),
    paddingLeft: Math.max(horizontal * s, insets.left),
    paddingRight: Math.max(horizontal * s, insets.right),
  };
}
