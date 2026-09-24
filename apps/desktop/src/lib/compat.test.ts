/**
 * Globals newer than the browsers this is built for.
 *
 * The web build targets `chrome87`, but a build target only rewrites *syntax*.
 * A global that does not exist on the device ships untouched and throws at the
 * moment it is called — and because that call is usually inside a render, React
 * unmounts the whole tree and the screen goes the colour of the page
 * background. On a wall panel with no console, every cause looks identical:
 * black.
 *
 * `structuredClone` cost several rounds of guessing exactly this way. This
 * catches the next one at commit time instead.
 */

import { readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/** Call sites, not mentions — a comment explaining the ban is not a use. */
const TOO_NEW: Array<{ pattern: RegExp; name: string; since: string }> = [
  { pattern: /\bstructuredClone\s*\(/, name: 'structuredClone()', since: 'Chrome 98' },
  { pattern: /\bObject\.hasOwn\s*\(/, name: 'Object.hasOwn()', since: 'Chrome 93' },
  { pattern: /\bObject\.groupBy\s*\(/, name: 'Object.groupBy()', since: 'Chrome 117' },
  { pattern: /\bMap\.groupBy\s*\(/, name: 'Map.groupBy()', since: 'Chrome 117' },
  { pattern: /\bArray\.fromAsync\s*\(/, name: 'Array.fromAsync()', since: 'Chrome 121' },
  { pattern: /\.at\s*\(/, name: '.at()', since: 'Chrome 92' },
  { pattern: /\.findLast(Index)?\s*\(/, name: '.findLast()', since: 'Chrome 97' },
  { pattern: /\.toSorted\s*\(/, name: '.toSorted()', since: 'Chrome 110' },
  { pattern: /\.toReversed\s*\(/, name: '.toReversed()', since: 'Chrome 110' },
  { pattern: /\.toSpliced\s*\(/, name: '.toSpliced()', since: 'Chrome 110' },
  // Not a version problem — these exist in every modern browser and are absent
  // on `http://192.168.x.x`, which is every device this is actually served to.
  // Loopback is a secure context, so testing there hides them completely.
  { pattern: /\bcrypto\.randomUUID\s*\(/, name: 'crypto.randomUUID()', since: 'a secure context' },
  { pattern: /\bcrypto\.subtle\b/, name: 'crypto.subtle', since: 'a secure context' },
  { pattern: /\bnavigator\.clipboard\b/, name: 'navigator.clipboard', since: 'a secure context' },
];

// Vitest runs from the package root.
const ROOT = resolve(process.cwd(), 'src');

function sources(directory: string): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) {
      found.push(...sources(path));
      continue;
    }
    // Tests run in Node, which is current; only shipped code has to be careful.
    if (entry.endsWith('.test.ts') || entry.endsWith('.test.tsx')) continue;
    if (['.ts', '.tsx'].includes(extname(entry))) found.push(path);
  }
  return found;
}

describe('browser compatibility', () => {
  it('ships nothing newer than the build target', () => {
    const offences: string[] = [];

    for (const file of sources(ROOT)) {
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, index) => {
        // Not a parser: enough to skip the comments that explain why these are
        // banned, which would otherwise be the only thing this ever reports.
        const code = line.trim();
        if (code.startsWith('*') || code.startsWith('//') || code.startsWith('/*')) return;
        for (const { pattern, name, since } of TOO_NEW) {
          if (pattern.test(line)) {
            const where = `${file.slice(ROOT.length + 1)}:${index + 1}`;
            offences.push(`${where} uses ${name}, which needs ${since}`);
          }
        }
      });
    }

    expect(offences).toEqual([]);
  });
});
