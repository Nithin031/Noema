// Rope, gauge and loop visualisations, ported from the riso design reference.
// All three are presentational: they draw whatever drift value they are handed.
import type { ReactElement } from 'react';
import { clamp, type Kind } from '../lib/adapt';
import { useAnimClock } from '../hooks/useAnimClock';
import type { Drive } from './drive';

const mix = (k: number) => {
  const a = [43, 75, 255], b = [255, 91, 31];
  return `rgb(${a.map((v, i) => Math.round(v + (b[i] - v) * k)).join(',')})`;
};

// ---- rope ----------------------------------------------------------------

export type RopeEpisode = { app: string; title: string; kind: Kind; fresh?: boolean };

export function RopeViz({ drive, episodes, calmFrozen }: { drive: Drive; episodes: RopeEpisode[]; calmFrozen?: boolean }) {
  const nowMs = useAnimClock();
  const { d, t, snap } = drive;
  const sway = (calmFrozen ? 0 : (1 - d) * Math.sin((nowMs / 1000) * 1.6) * 40);
  const P0 = [180, 380], P3 = [2200 + d * 70, 380 + sway * 0.5];
  const P1 = [820, 380 + (1 - d) * 300 + sway], P2 = [1560, 380 - (1 - d) * 140 + sway * 1.3];
  const B = (u: number) => {
    const m = 1 - u;
    return [0, 1].map((i) => m * m * m * P0[i] + 3 * m * m * u * P1[i] + 3 * m * u * u * P2[i] + u * u * u * P3[i]);
  };
  const col = mix(clamp((d - 0.42) / 0.3));
  const path = (a: number, b: number, f?: (u: number, p: number[]) => number[]) => {
    const o: number[][] = [];
    for (let i = 0; i <= 40; i++) { const u = a + ((b - a) * i) / 40; const p = B(u); o.push(f ? f(u, p) : p); }
    return 'M' + o.map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' L');
  };
  const Rope = ({ dd, op }: { dd: string; op: number }) => (
    <g opacity={op}>
      <path d={dd} fill="none" stroke="#161411" strokeWidth={30} strokeLinecap="round" />
      <path d={dd} fill="none" stroke={col} strokeWidth={16} strokeLinecap="round" />
      <path d={dd} fill="none" stroke="#F3ECDD" strokeWidth={3} strokeDasharray="16 18" opacity={0.8} />
    </g>
  );

  let shake = 0;
  const ropes: ReactElement[] = [];
  if (snap >= 0) {
    const s = 1 - Math.pow(1 - clamp(snap / 0.5), 3);
    shake = Math.sin(snap * 70) * 22 * (1 - clamp(snap / 0.4));
    const fade = 1 - clamp((snap - 0.55) / 0.45);
    ropes.push(<Rope key="L" dd={path(0, 0.58, (u, p) => [p[0], p[1] + s * 260 * Math.pow(u / 0.58, 2)])} op={fade} />);
    ropes.push(<Rope key="R" dd={path(0.62, 1, (u, p) => [p[0] + s * 90, p[1] + s * 300 * Math.pow((1 - u) / 0.38, 2)])} op={fade} />);
    ropes.push(<Rope key="N" dd={path(0, 1)} op={clamp((snap - 0.55) / 0.45)} />);
  } else ropes.push(<Rope key="N" dd={path(0, 1)} op={1} />);

  const shown = episodes.slice(-6);
  const spots = shown.map((_, i) => 0.1 + (i * 0.76) / Math.max(1, shown.length - 1 || 1));
  const nf = t >= 10 && t < 12.4 ? clamp((t - 10) / 0.25) * (1 - clamp((t - 12) / 0.4)) : 0;
  const nsc = 1 + 1.4 * Math.pow(1 - clamp((t - 10) / 0.3), 3);

  return (
    <svg viewBox="0 0 2420 760" width="100%" style={{ display: 'block', transform: `translateX(${shake}px)` }} role="img"
      aria-label={`Rope from your intent to what you are doing now. Tension ${d.toFixed(2)}.`}>
      {ropes}
      {shown.map((e, i) => {
        const u = shown.length === 1 ? 0.5 : spots[i];
        const p = B(u), up = i % 2 === 0, sy = up ? -150 : 110;
        const lab = e.kind === 'aligned' ? '✓ ALIGNED' : e.kind === 'unknown' ? '? UNKNOWN' : '! DRIFTING';
        const w = lab.length * 21 + 44;
        const isD = e.kind === 'drifting';
        const text = `${e.app}${e.title ? ' · ' + e.title : ''}`.slice(0, 34);
        return (
          <g key={i}>
            <line x1={p[0]} y1={p[1]} x2={p[0]} y2={p[1] + sy + (up ? 70 : 0)} stroke="#161411" strokeWidth={4} />
            <circle cx={p[0]} cy={p[1]} r={26} fill="#161411" />
            <circle cx={p[0]} cy={p[1]} r={11} fill={isD ? '#FF5B1F' : e.kind === 'aligned' ? '#2B4BFF' : '#F3ECDD'} />
            <g transform={`translate(${p[0]} ${p[1] + sy}) rotate(${up ? -4 : 3})`}>
              <rect x={-w / 2} y={0} width={w} height={66} fill={isD ? '#FF5B1F' : '#FFFDF6'} stroke={e.kind === 'aligned' ? '#2B4BFF' : '#161411'} strokeWidth={6} strokeDasharray={e.kind === 'unknown' ? '14 8' : 'none'} />
              <text x={0} y={45} textAnchor="middle" fontFamily="DM Mono" fontWeight={500} fontSize={32} letterSpacing={3} fill={e.kind === 'aligned' ? '#2B4BFF' : '#161411'}>{lab}</text>
              <text x={0} y={up ? -16 : 106} textAnchor="middle" fontFamily="DM Mono" fontSize={28} fill="#161411">{text}</text>
            </g>
          </g>
        );
      })}
      <g transform={`translate(${P0[0] - 110} ${P0[1] - 110})`}>
        <rect x={0} y={0} width={110} height={220} fill="#2B4BFF" stroke="#161411" strokeWidth={6} />
        <text x={55} y={120} textAnchor="middle" fontFamily="DM Mono" fontSize={26} fill="#F3ECDD" transform="rotate(-90 55 110)">INTENT</text>
      </g>
      <g transform={`translate(${P3[0]} ${P3[1]})`}>
        <circle r={52} fill="#FFFDF6" stroke="#161411" strokeWidth={7} />
        <text y={11} textAnchor="middle" fontFamily="DM Mono" fontWeight={500} fontSize={30} fill="#161411">YOU</text>
      </g>
      <g opacity={nf} transform={`translate(1200 120) rotate(-6) scale(${nsc})`}>
        <rect x={-200} y={-50} width={400} height={100} fill="#FF5B1F" stroke="#161411" strokeWidth={7} />
        <text y={16} textAnchor="middle" fontFamily="DM Mono" fontWeight={500} fontSize={44} letterSpacing={4} fill="#161411">SNAP · NUDGE</text>
      </g>
      <text x={2380} y={700} textAnchor="end" fontFamily="DM Mono" fontSize={30} fill="#161411">tension {d.toFixed(2)}</text>
    </svg>
  );
}
