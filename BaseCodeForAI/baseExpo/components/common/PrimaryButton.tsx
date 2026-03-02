import { ReactNode } from 'react';
import { Pressable, PressableProps, StyleSheet, Text } from 'react-native';

type PrimaryButtonProps = PressableProps & {
  label?: string;
  children?: ReactNode;
};

export function PrimaryButton({ label, children, style, ...rest }: PrimaryButtonProps) {
  return (
    <Pressable style={({ pressed }) => [styles.button, pressed && styles.pressed, style]} {...rest}>
      <Text style={styles.label}>{label ?? children}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  button: {
    paddingVertical: 12,
    paddingHorizontal: 16,
    borderRadius: 8,
    backgroundColor: '#2563eb',
    alignItems: 'center',
    justifyContent: 'center',
  },
  pressed: {
    opacity: 0.8,
  },
  label: {
    color: '#fff',
    fontSize: 16,
    fontWeight: '500',
  },
});

