import { Text, TextProps, StyleSheet } from 'react-native';

type AppTextVariant = 'title' | 'body';

type AppTextProps = TextProps & {
  variant?: AppTextVariant;
};

export function AppText({ variant = 'body', style, ...rest }: AppTextProps) {
  return <Text style={[styles.base, variantStyles[variant], style]} {...rest} />;
}

const styles = StyleSheet.create({
  base: {
    color: '#000',
  },
  title: {
    fontSize: 24,
    fontWeight: '600',
    marginBottom: 8,
  },
  body: {
    fontSize: 16,
  },
});

const variantStyles: Record<AppTextVariant, object> = {
  title: styles.title,
  body: styles.body,
};

