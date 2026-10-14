import type { TextStyle } from 'react-native';

export const colors = {
  ink: '#0E0F0C',
  muted: '#5A5C57',
  pressedInk: '#2A2B28',

  surface: '#FFFFFF',
  subtle: '#F7F6F3',
  neutral: '#F2F1ED',
  zone: '#ECEAE4',
  track: '#DCDAD3',
  border: '#E4E2DC',
  guide: '#B9B7B0',

  goodBg: '#E3F2E8',
  goodFg: '#0B5E30',
  alert: '#C8410B',
  onAlert: '#FFFFFF',

  term: {
    bg: '#0E0F0C',
    border: '#2A2B28',
    buttonBorder: '#3A3B37',
    text: '#E7E5DF',
    msg: '#C9C7C0',
    dim: '#6E706A',
    dimmer: '#8A8C86',
    ok: '#7FD1A0',
    warn: '#F08A5D',
  },

  noSignalVeil: 'rgba(247,246,243,0.82)',
} as const;

export const radii = {
  landingButton: 22,
  card: 16,
  /** Coxswain feedback boxes and portrait buttons. */
  control: 14,
  /** Coxswain landscape lanes and buttons. */
  lane: 12,
  /** Coxswain portrait lanes. */
  laneCompact: 10,
  pill: 12,
  terminalButton: 6,
} as const;

// Custom fonts must be selected by family name; fontWeight is ignored for them on Android.
export const fonts = {
  medium: 'Barlow_500Medium',
  semibold: 'Barlow_600SemiBold',
  bold: 'Barlow_700Bold',
  extrabold: 'Barlow_800ExtraBold',
  mono: 'JetBrainsMono_400Regular',
  monoSemibold: 'JetBrainsMono_600SemiBold',
} as const;

export const tabular: TextStyle = { fontVariant: ['tabular-nums'] };

// Every size in the design is specified for this landscape screen (or its portrait rotation).
export const DESIGN_WIDTH = 844;
export const DESIGN_HEIGHT = 390;
