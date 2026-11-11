export type PageKey = 'command' | 'analytics' | 'memes' | 'engine' | 'week';

export const PAGES: [PageKey, string][] = [
  ['command', 'Command'], ['analytics', 'Analytics'], ['memes', 'Memes'], ['engine', 'Engine room'], ['week', 'Week'],
];

export type Route = PageKey | 'recovery' | 'break' | 'demo';
const ROUTES = new Set<string>([...PAGES.map(([k]) => k), 'recovery', 'break', 'demo']);

export function parseRoute(hash: string): Route {
  const key = hash.replace(/^#\/?/, '').split(/[/?]/)[0];
  return (ROUTES.has(key) ? key : 'command') as Route;
}
