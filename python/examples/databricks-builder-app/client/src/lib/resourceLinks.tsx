import React from 'react';

/**
 * Extract workspace URL from environment or config
 */
export function getWorkspaceUrl(): string {
  // This should match the workspaceUrl used elsewhere in the app
  // For now, we'll parse it from the Catalog Explorer link or use a default
  return 'https://fevm-serverless-dxukih.cloud.databricks.com';
}

/**
 * Clean intermediate planning text from agent responses
 * Removes "Let me..." planning statements that appear before the final answer
 */
export function cleanPlanningText(text: string): string {
  // Split by common delimiters that indicate the start of the actual answer
  const markers = [
    'Perfect!',
    'Great!',
    'Done!',
    'Here',
    'I\'ve created',
    'I\'ll create',
    'The tables are',
    'Good progress!',
  ];
  
  // Try to find where the actual response starts
  let cleanedText = text;
  
  for (const marker of markers) {
    const index = text.indexOf(marker);
    if (index > 0) {
      // Check if there's a lot of "Let me..." text before this marker
      const before = text.substring(0, index);
      const letMeCount = (before.match(/Let me /gi) || []).length;
      
      // If there are multiple "Let me" statements, this is likely planning text
      if (letMeCount >= 2) {
        cleanedText = text.substring(index);
        break;
      }
    }
  }
  
  return cleanedText;
}

/**
 * Linkify Databricks resources in text
 * Converts table names, catalog.schema, volume paths to clickable links
 */
export function linkifyDatabricksResources(
  text: string,
  workspaceUrl: string,
  defaultCatalog?: string,
  defaultSchema?: string
): React.ReactNode {
  // Pattern to match: catalog.schema.table or catalog.schema or /Volumes/catalog/schema/volume
  const patterns = [
    // Match /Volumes/catalog/schema/volume_name
    {
      regex: /\/Volumes\/([a-zA-Z0-9_-]+)\/([a-zA-Z0-9_-]+)\/([a-zA-Z0-9_-]+)/g,
      buildUrl: (match: RegExpMatchArray) => 
        `${workspaceUrl}/explore/data/volumes/${match[1]}/${match[2]}/${match[3]}`,
      title: (match: RegExpMatchArray) => `Open volume ${match[3]} in Catalog Explorer`,
    },
    // Match catalog.schema.table (three parts)
    {
      regex: /\b([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)\b/g,
      buildUrl: (match: RegExpMatchArray) => 
        `${workspaceUrl}/explore/data/${match[1]}/${match[2]}/${match[3]}`,
      title: (match: RegExpMatchArray) => `Open table ${match[3]} in Catalog Explorer`,
    },
    // Match catalog.schema (two parts) - only if not followed by another dot
    {
      regex: /\b([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)\b(?!\.)/g,
      buildUrl: (match: RegExpMatchArray) => 
        `${workspaceUrl}/explore/data/${match[1]}/${match[2]}`,
      title: (match: RegExpMatchArray) => `Open schema ${match[2]} in Catalog Explorer`,
    },
  ];

  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  const matches: Array<{start: number; end: number; url: string; text: string; title: string}> = [];

  // Find all matches across all patterns
  patterns.forEach(pattern => {
    let match;
    while ((match = pattern.regex.exec(text)) !== null) {
      matches.push({
        start: match.index,
        end: match.index + match[0].length,
        url: pattern.buildUrl(match),
        text: match[0],
        title: pattern.title(match),
      });
    }
  });

  // Sort matches by position
  matches.sort((a, b) => a.start - b.start);

  // Remove overlapping matches (keep the first one)
  const filteredMatches = matches.filter((match, index) => {
    if (index === 0) return true;
    const prevMatch = matches[index - 1];
    return match.start >= prevMatch.end;
  });

  // Build the result with links
  filteredMatches.forEach((match, index) => {
    // Add text before the match
    if (match.start > lastIndex) {
      parts.push(text.substring(lastIndex, match.start));
    }

    // Add the link
    parts.push(
      <a
        key={`link-${index}`}
        href={match.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-[var(--color-accent-primary)] hover:underline font-medium inline-flex items-center gap-1"
        title={match.title}
      >
        {match.text}
        <svg className="w-3 h-3 opacity-70" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
        </svg>
      </a>
    );

    lastIndex = match.end;
  });

  // Add remaining text
  if (lastIndex < text.length) {
    parts.push(text.substring(lastIndex));
  }

  return parts.length > 0 ? <>{parts}</> : text;
}
