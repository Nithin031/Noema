export type PageKey = 'today' | 'week' | 'models';

export const PAGES: [PageKey, string][] = [
  ['today', 'Today'], ['week', 'Week'], ['models', 'Models'],
];

export type Route = PageKey;
const ROUTES = new Set<string>(PAGES.map(([k]) => k));

export function parseRoute(hash: string): Route {
  const key = hash.replace(/^#\/?/, '').split(/[/?]/)[0];
  return (ROUTES.has(key) ? key : 'today') as Route;
}
