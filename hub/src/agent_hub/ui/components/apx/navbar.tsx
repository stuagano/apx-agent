import { ReactNode } from "react";
import { useDesignSystemTheme } from "@databricks/design-system";
import { ModeToggle } from "@/components/apx/mode-toggle";
import Logo from "@/components/apx/logo";

interface NavbarProps {
  leftContent?: ReactNode;
  rightContent?: ReactNode;
}

export function Navbar({ leftContent, rightContent }: NavbarProps) {
  const { theme } = useDesignSystemTheme();
  return (
    <header
      style={{
        background: theme.colors.backgroundPrimary,
        borderBottom: `1px solid ${theme.colors.border}`,
      }}
    >
      <div
        style={{
          height: 64,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: `0 ${theme.spacing.md}px`,
        }}
      >
        {leftContent || <Logo />}
        <div style={{ flex: 1 }} />
        {rightContent || <ModeToggle />}
      </div>
    </header>
  );
}

export default Navbar;
