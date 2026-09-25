import { Button, MoonIcon, SunIcon } from "@databricks/design-system";
import { useTheme } from "@/components/apx/theme-provider";
import { renderIcon } from "@/components/apx/Icon";

export function ModeToggle() {
  const { theme, setTheme } = useTheme();
  return (
    <Button
      componentId="mode-toggle"
      type="tertiary"
      icon={renderIcon(theme === "light" ? MoonIcon : SunIcon)}
      onClick={() => setTheme(theme === "light" ? "dark" : "light")}
      aria-label="Toggle theme"
    />
  );
}
