import { ReactNode } from 'react';
import { SafeAreaView, StyleSheet, ViewStyle } from 'react-native';

type ScreenContainerProps = {
  children: ReactNode;
  style?: ViewStyle | ViewStyle[];
};

export function ScreenContainer({ children, style }: ScreenContainerProps) {
  return <SafeAreaView style={[styles.container, style]}>{children}</SafeAreaView>;
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    padding: 16,
  },
});

