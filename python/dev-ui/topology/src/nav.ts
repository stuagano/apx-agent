/** Keep aligned with APX_NAV_PAGES in _ui_nav.py (no emoji chrome). */
export const NAV_LINKS: ReadonlyArray<{ href: string; label: string; slug: string }> = [
  { href: "/_apx/agent", label: "Chat", slug: "agent" },
  { href: "/_apx/edit", label: "Edit", slug: "edit" },
  { href: "/_apx/eval", label: "Eval", slug: "eval" },
  { href: "/_apx/discover", label: "Discover", slug: "discover" },
  { href: "/_apx/grounding", label: "Grounding", slug: "grounding" },
  { href: "/_apx/probe", label: "Probe", slug: "probe" },
  { href: "/_apx/topology", label: "Topology", slug: "topology" },
];
