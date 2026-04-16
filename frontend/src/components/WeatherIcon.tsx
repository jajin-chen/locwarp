import React from 'react';

/**
 * Animated weather icons — the "動態微動畫 (Set 04)" design.
 *
 * Matches Open-Meteo's WMO weather_code. Each variant is a small inline SVG
 * with a subtle animation (sun rays pulse, rain drops fall, snow rotates,
 * lightning flashes) so the user notices current conditions changing. Size
 * defaults to 16 — matches the chip preview in the mockup.
 */

export type WeatherCat =
  | 'clear'
  | 'partly'
  | 'cloudy'
  | 'fog'
  | 'drizzle'
  | 'rain'
  | 'snow'
  | 'storm';

export function categorize(code: number | null | undefined): WeatherCat | null {
  if (code == null) return null;
  if (code === 0) return 'clear';
  if (code === 1 || code === 2) return 'partly';
  if (code === 3) return 'cloudy';
  if (code === 45 || code === 48) return 'fog';
  if ((code >= 51 && code <= 57)) return 'drizzle';
  if ((code >= 61 && code <= 67) || (code >= 80 && code <= 82)) return 'rain';
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return 'snow';
  if (code === 95 || code === 96 || code === 99) return 'storm';
  return 'cloudy';
}

import type { StringKey } from '../i18n';

/** Return the i18n key for the chip label; caller runs it through `useT()`. */
export function labelKeyFor(cat: WeatherCat | null): StringKey | null {
  switch (cat) {
    case 'clear':   return 'weather.clear';
    case 'partly':  return 'weather.partly';
    case 'cloudy':  return 'weather.cloudy';
    case 'fog':     return 'weather.fog';
    case 'drizzle': return 'weather.drizzle';
    case 'rain':    return 'weather.rain';
    case 'snow':    return 'weather.snow';
    case 'storm':   return 'weather.storm';
    default:        return null;
  }
}

interface Props {
  cat: WeatherCat;
  size?: number;
}

export const WeatherIcon: React.FC<Props> = ({ cat, size = 16 }) => {
  const stroke = 2;
  const base = '#c8d0e0';
  const yellow = '#ffb74d';
  const blue = '#6ba2ff';
  const white = '#ffffff';
  const bolt = '#ffeb3b';

  // Shared cloud path used by almost everything — matches mockup Set 04.
  const cloudPath = 'M16 13H7a4 4 0 1 1 0-8c.25-1.5 1-3 2.5-3A4.5 4.5 0 0 1 14 6.5c0 .5-.05 1-.14 1.5H16a2.5 2.5 0 1 1 0 5z';
  // Cloud shifted down, used when the icon needs room above for sun/below for rain.
  const cloudPathLow = 'M16 17H7a4 4 0 1 1 0-8c.25-1.5 1-3 2.5-3A4.5 4.5 0 0 1 14 10.5c0 .5-.05 1-.14 1.5H16a2.5 2.5 0 1 1 0 5z';

  const svgProps = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none' as const,
    stroke: base,
    strokeWidth: stroke,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
  };

  switch (cat) {
    case 'clear':
      return (
        <svg {...svgProps} stroke={yellow}>
          <circle cx="12" cy="12" r="4" />
          <g className="wx-sun-rays">
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
          </g>
        </svg>
      );
    case 'partly':
      return (
        <svg {...svgProps}>
          <g stroke={yellow}>
            <circle cx="12" cy="10" r="3" />
            <g className="wx-sun-rays">
              <path d="M12 2v2M4.93 4.93l1.41 1.41M2 10h2M19.07 4.93l-1.41 1.41" />
            </g>
          </g>
          <path d="M21 18.5A3.5 3.5 0 0 1 17.5 22h-11A5.5 5.5 0 1 1 12 12c2 0 3.5 1.12 4.2 2.65A3.5 3.5 0 0 1 21 18.5z" />
        </svg>
      );
    case 'cloudy':
      return (
        <svg {...svgProps}>
          <path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9z" />
        </svg>
      );
    case 'fog':
      return (
        <svg {...svgProps}>
          <path d={cloudPathLow} />
          <line x1="3" y1="20" x2="21" y2="20" />
          <line x1="6" y1="23" x2="18" y2="23" />
        </svg>
      );
    case 'drizzle':
      return (
        <svg {...svgProps}>
          <path d={cloudPath} />
          <g stroke={blue}>
            <line x1="8" y1="18" x2="8" y2="20" className="wx-rain-drop" />
            <line x1="12" y1="18" x2="12" y2="20" className="wx-rain-drop wx-delay-1" />
            <line x1="16" y1="18" x2="16" y2="20" className="wx-rain-drop wx-delay-2" />
          </g>
        </svg>
      );
    case 'rain':
      return (
        <svg {...svgProps}>
          <path d={cloudPath} />
          <g stroke={blue} strokeWidth="2.2">
            <line x1="8" y1="17" x2="7" y2="22" className="wx-rain-drop" />
            <line x1="13" y1="17" x2="12" y2="22" className="wx-rain-drop wx-delay-1" />
            <line x1="18" y1="17" x2="17" y2="22" className="wx-rain-drop wx-delay-2" />
          </g>
        </svg>
      );
    case 'snow':
      return (
        <svg {...svgProps}>
          <path d={cloudPath} />
          <g stroke={white} strokeWidth="2.4">
            <line x1="8" y1="18" x2="8.01" y2="18" className="wx-snow-flake" />
            <line x1="8" y1="22" x2="8.01" y2="22" className="wx-snow-flake wx-delay-1" />
            <line x1="12" y1="18" x2="12.01" y2="18" className="wx-snow-flake wx-delay-2" />
            <line x1="12" y1="22" x2="12.01" y2="22" className="wx-snow-flake wx-delay-3" />
            <line x1="16" y1="18" x2="16.01" y2="18" className="wx-snow-flake wx-delay-1" />
            <line x1="16" y1="22" x2="16.01" y2="22" className="wx-snow-flake wx-delay-2" />
          </g>
        </svg>
      );
    case 'storm':
      return (
        <svg {...svgProps}>
          <path d={cloudPath} />
          <polyline
            points="13 15 11 19 14 19 12 23"
            stroke={bolt}
            className="wx-bolt"
          />
        </svg>
      );
  }
};
