import { Link } from "@tanstack/react-router";

interface LogoProps {
  to?: string;
  className?: string;
}

export function Logo({ to = "/", className = "" }: LogoProps) {
  const content = (
    <div className={`flex items-center gap-2 ${className}`}>
      <img src="/logo.svg" alt="logo" className="h-6 w-6 border border-primary rounded-sm" />
      <span className="font-semibold text-lg">Agent Hub</span>
    </div>
  );
  return (
    <Link to={to} className="hover:opacity-80 transition-opacity">
      {content}
    </Link>
  );
}

export default Logo;
