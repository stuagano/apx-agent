import { createContext, useContext, useEffect, useState } from "react";
import {
  DesignSystemProvider,
  DesignSystemThemeProvider,
  useDesignSystemTheme,
} from "@databricks/design-system";

type Theme = "dark" | "light";

type ThemeProviderState = {
  theme: Theme;
  setTheme: (theme: Theme) => void;
};

const ThemeProviderContext = createContext<ThemeProviderState | undefined>(
  undefined,
);

function initialTheme(storageKey: string, fallback: Theme): Theme {
  const stored = localStorage.getItem(storageKey);
  if (stored === "light" || stored === "dark") return stored;
  return fallback;
}

/**
 * Drives Du Bois's light/dark theme and persists the choice.
 * Wraps DesignSystemThemeProvider (picks token values) + DesignSystemProvider
 * (serves the theme to useDesignSystemTheme). The two Du Bois stylesheets are
 * imported once in main.tsx.
 */
export function ThemeProvider({
  children,
  defaultTheme = "dark",
  storageKey = "apx-ui-theme",
}: {
  children: React.ReactNode;
  defaultTheme?: Theme;
  storageKey?: string;
}) {
  const [theme, setThemeState] = useState<Theme>(() =>
    initialTheme(storageKey, defaultTheme),
  );

  useEffect(() => {
    localStorage.setItem(storageKey, theme);
  }, [theme, storageKey]);

  return (
    <ThemeProviderContext.Provider value={{ theme, setTheme: setThemeState }}>
      <DesignSystemThemeProvider isDarkMode={theme === "dark"}>
        <DesignSystemProvider>
          <BodyBackground />
          {children}
        </DesignSystemProvider>
      </DesignSystemThemeProvider>
    </ThemeProviderContext.Provider>
  );
}

// Paints the page background from the Du Bois token so there's no white flash
// behind the app shell. Lives inside DesignSystemProvider so the token resolves.
function BodyBackground() {
  const { theme } = useDesignSystemTheme();
  useEffect(() => {
    document.body.style.backgroundColor = theme.colors.backgroundPrimary;
  }, [theme.colors.backgroundPrimary]);
  return null;
}

export function useTheme() {
  const context = useContext(ThemeProviderContext);
  if (context === undefined)
    throw new Error("useTheme must be used within a ThemeProvider");
  return context;
}
