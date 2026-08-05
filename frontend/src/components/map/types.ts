export interface Position {
  lat: number;
  lng: number;
}

export interface Waypoint {
  lat: number;
  lng: number;
  index: number;
}

export interface ContextMenuState {
  visible: boolean;
  x: number;
  y: number;
  lat: number;
  lng: number;
}

export interface WpMenuState {
  visible: boolean;
  x: number;
  y: number;
  index: number;
  isStart: boolean;
}

import type { MutableRefObject } from 'react';

// Translation ref shared by the once-mounted Leaflet handlers and the
// extracted map sub-components: lookups route through the ref so language
// switches reach handlers captured at mount time.
export type TRef = MutableRefObject<(k: any, v?: any) => string>;
