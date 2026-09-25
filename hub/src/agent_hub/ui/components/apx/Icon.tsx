import type { ComponentType, ReactElement } from "react";

// Du Bois icon components are typed as forwardRef components whose props include
// required onPointerEnterCapture/onPointerLeaveCapture handlers, so rendering a
// bare <SomeIcon /> as a prop value (e.g. Button `icon`, Input `prefix`) fails
// typecheck. This helper renders any Du Bois icon by casting to a loose prop
// type, isolating the quirk in ONE place. (Per the fde-databricks-app skill.)
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function renderIcon(
  IconComponent: ComponentType<any>,
  props: Record<string, unknown> = {},
): ReactElement {
  const Any = IconComponent;
  return <Any {...props} />;
}
