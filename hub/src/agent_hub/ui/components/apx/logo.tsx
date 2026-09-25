import { Link } from "@tanstack/react-router";
import { Typography, useDesignSystemTheme } from "@databricks/design-system";

interface LogoProps {
  to?: string;
  showText?: boolean;
}

export function Logo({ to = "/", showText = true }: LogoProps) {
  const { theme } = useDesignSystemTheme();

  const content = (
    <div
      style={{ display: "flex", alignItems: "center", gap: theme.spacing.sm }}
    >
      <img
        src="/logo.svg"
        alt="logo"
        style={{
          height: 24,
          width: 24,
          border: `1px solid ${theme.colors.actionPrimaryBackgroundDefault}`,
          borderRadius: theme.borders.borderRadiusSm,
        }}
      />
      {showText && (
        <Typography.Title level={4} withoutMargins>
          {__APP_NAME__}
        </Typography.Title>
      )}
    </div>
  );

  if (to) {
    return (
      <Link to={to} style={{ textDecoration: "none" }}>
        {content}
      </Link>
    );
  }
  return content;
}

export default Logo;
