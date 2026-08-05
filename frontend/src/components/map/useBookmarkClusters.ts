import { useEffect, useRef } from 'react';
import type { MutableRefObject } from 'react';
import L from 'leaflet';
import Supercluster from 'supercluster';
import { escapeHtml } from './utils';

export interface BookmarkPin {
  id?: string;
  name: string;
  lat: number;
  lng: number;
  country_code?: string;
}

// Render/clear small bookmark pins on the map when the user toggles
// 'show all bookmarks on map'. Each pin is clickable and teleports to
// that bookmark's position.
export function useBookmarkClusters({
  mapRef,
  bookmarkPins,
  showBookmarkPins,
  onTeleportRef,
}: {
  mapRef: MutableRefObject<L.Map | null>;
  bookmarkPins?: BookmarkPin[];
  showBookmarkPins?: boolean;
  // Routed through a ref so this effect doesn't rebuild the (potentially
  // 50k-point) supercluster index just because the parent passed a new
  // onTeleport closure.
  onTeleportRef: MutableRefObject<(lat: number, lng: number, source?: 'menu' | 'coord') => void>;
}): void {
  const bookmarkMarkersRef = useRef<L.Marker[]>([]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    bookmarkMarkersRef.current.forEach((m) => m.remove());
    bookmarkMarkersRef.current = [];
    if (!showBookmarkPins || !bookmarkPins || bookmarkPins.length === 0) return;

    // Scalable clustering via supercluster (O(n log n) index) + viewport
    // culling. The old hand-rolled clusterer was O(n²) and built a DOM marker
    // for every bookmark regardless of what was on screen — fine for a few
    // hundred, but it freezes the UI thread at tens of thousands of points.
    // Now we index once, then on every pan/zoom render ONLY the clusters /
    // leaves that fall inside the current viewport, so the map never holds
    // more than a few dozen markers at a time no matter how many are saved.
    const index = new Supercluster({ radius: 60, maxZoom: 18, minPoints: 2 });
    index.load(
      bookmarkPins!.map((bm, i) => ({
        type: 'Feature' as const,
        properties: { idx: i },
        geometry: { type: 'Point' as const, coordinates: [bm.lng, bm.lat] },
      })),
    );

    const render = () => {
      bookmarkMarkersRef.current.forEach((m) => m.remove());
      bookmarkMarkersRef.current = [];
      const b = map.getBounds();
      const bbox: [number, number, number, number] = [
        b.getWest(), b.getSouth(), b.getEast(), b.getNorth(),
      ];
      const zoom = Math.round(map.getZoom());
      const features = index.getClusters(bbox, zoom);

      features.forEach((f: any) => {
        const [lng, lat] = f.geometry.coordinates as [number, number];
        const isCluster = !!f.properties.cluster;

        if (!isCluster) {
          const bm = bookmarkPins![f.properties.idx as number];
          const flagHtml = bm.country_code
            ? `<img src="https://flagcdn.com/w20/${bm.country_code}.png" style="width:18px;height:12px;border-radius:2px;flex-shrink:0;display:inline-block;vertical-align:middle;" alt="" />`
            : '';
          // Design 5 — Neon glass bubble. Frosted capsule with purple glow,
          // flag + name inside, tiny pointing nub underneath pinning the
          // coordinate. Max width 180px, name truncates with ellipsis.
          const icon = L.divIcon({
            className: 'bookmark-pin',
            // Outer div fills the Leaflet divIcon container, flex column
            // bottom-center so the glowing dot at the bottom sits exactly
            // on the (lat, lng) coordinate (matches iconAnchor below).
            html: `<div style="width:100%;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;pointer-events:none;">
              <div style="
                padding:5px 12px 5px 6px;
                border-radius:100px;
                background:linear-gradient(135deg, rgba(255,255,255,0.88), rgba(255,255,255,0.68));
                color:#0e0f10;
                font-size:12px;font-weight:600;line-height:1.2;
                box-shadow:
                  0 0 0 1px rgba(99,102,241,0.45),
                  0 0 14px rgba(99,102,241,0.4),
                  0 3px 8px rgba(0,0,0,0.15);
                display:inline-flex;align-items:center;gap:6px;
                max-width:180px;white-space:nowrap;overflow:hidden;
                backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);
                pointer-events:auto;cursor:pointer;
              ">${flagHtml}<span style="overflow:hidden;text-overflow:ellipsis;max-width:140px;">${escapeHtml(bm.name)}</span></div>
              <div style="
                width:10px;height:10px;margin-top:-5px;
                background:linear-gradient(135deg, rgba(255,255,255,0.88), rgba(255,255,255,0.68));
                transform:rotate(45deg);
                box-shadow:2px 2px 6px rgba(99,102,241,0.3);
                border-right:1px solid rgba(99,102,241,0.45);
                border-bottom:1px solid rgba(99,102,241,0.45);
              "></div>
              <div style="width:5px;height:5px;border-radius:50%;background:rgba(99,102,241,0.7);margin-top:-3px;box-shadow:0 0 8px rgba(99,102,241,0.9);"></div>
            </div>`,
            iconSize: [200, 56],
            // Anchor = bottom-center of the icon = the glowing dot = exact
            // (lat, lng) coordinate. Previously the flex-inline column was
            // sitting at top-left so the whole pin rendered above-left of
            // the real point.
            iconAnchor: [100, 56],
          });
          const marker = L.marker([bm.lat, bm.lng], {
            icon,
            pane: 'markerPane',
            // Sit above the blue person marker (zIndexOffset 1000) so the
            // pin stays clickable when the user is standing on it.
            zIndexOffset: 2000,
          });
          marker.on('click', () => onTeleportRef.current(bm.lat, bm.lng));
          marker.addTo(map);
          bookmarkMarkersRef.current.push(marker);
        } else {
          // Design 4 — Polaroid stack cluster. Three overlapping mini cards
          // with rotation, top one shows the count.
          const count = f.properties.point_count as number;
          const countLabel = f.properties.point_count_abbreviated as string;
          const clusterId = f.properties.cluster_id as number;
          const icon = L.divIcon({
            className: 'bookmark-cluster-pin',
            html: `<div style="position:relative;width:52px;height:46px;pointer-events:none;">
              <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-8deg) translate(-4px, 3px);width:38px;height:32px;background:#fff;border:1px solid #c8ccd4;box-shadow:0 2px 6px rgba(0,0,0,0.3);"></div>
              <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(6deg) translate(4px, -2px);width:38px;height:32px;background:#fff;border:1px solid #c8ccd4;box-shadow:0 2px 6px rgba(0,0,0,0.3);"></div>
              <div style="
                position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
                width:38px;height:32px;background:#fff;border:1px solid #c8ccd4;
                box-shadow:0 2px 8px rgba(0,0,0,0.35);
                display:flex;align-items:center;justify-content:center;
                font-weight:700;font-size:15px;color:#2d3748;
                pointer-events:auto;cursor:pointer;
              ">${countLabel}</div>
              <div style="
                position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) translate(0, -14px);
                width:14px;height:3px;background:rgba(253,216,53,0.85);border-radius:1px;
                box-shadow:0 1px 2px rgba(0,0,0,0.2);
                z-index:3;
              "></div>
            </div>`,
            iconSize: [52, 46],
            iconAnchor: [26, 23],
          });
          const marker = L.marker([lat, lng], {
            icon,
            pane: 'markerPane',
            // Above blue person so the cluster card is always clickable.
            zIndexOffset: 2000,
          });

          // Small clusters (<= 12) open a clickable list so the user can pick
          // the exact bookmark — same UX as before. Bigger clusters just zoom
          // in to expand (a 5000-row popup is useless and slow to build).
          if (count <= 12) {
            const leaves = index.getLeaves(clusterId, count) as any[];
            const listHtml = leaves.map((leaf) => {
              const bm = bookmarkPins![leaf.properties.idx as number];
              const flag = bm.country_code
                ? `<img src="https://flagcdn.com/w20/${bm.country_code}.png" style="width:14px;height:10px;border-radius:1px;vertical-align:middle;margin-right:6px;" />`
                : '';
              return `<div
                class="bm-cluster-row"
                data-lat="${bm.lat}" data-lng="${bm.lng}"
                style="display:flex;align-items:center;gap:4px;padding:6px 8px;cursor:pointer;border-radius:4px;color:#e8e8ea;font-size:12px;transition:background 0.1s;"
                onmouseenter="this.style.background='rgba(255,255,255,0.08)'"
                onmouseleave="this.style.background='transparent'"
              >${flag}<span style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escapeHtml(bm.name)}</span></div>`;
            }).join('');
            const popup = L.popup({
              className: 'bookmark-cluster-popup',
              maxWidth: 240,
              offset: [0, -12],
            }).setContent(`
              <div style="background:rgba(26,29,39,0.96);backdrop-filter:blur(12px);border:1px solid rgba(108,140,255,0.25);border-radius:8px;padding:6px;min-width:180px;max-height:280px;overflow-y:auto;">
                <div style="padding:4px 8px;font-size:10px;letter-spacing:1px;text-transform:uppercase;color:#9ac0ff;">${count} ${escapeHtml(count === 1 ? 'bookmark' : 'bookmarks')}</div>
                ${listHtml}
              </div>
            `);
            marker.bindPopup(popup);
            marker.on('popupopen', () => {
              document.querySelectorAll('.bm-cluster-row').forEach((el) => {
                el.addEventListener('click', () => {
                  const lat2 = parseFloat((el as HTMLElement).dataset.lat || '');
                  const lng2 = parseFloat((el as HTMLElement).dataset.lng || '');
                  if (Number.isFinite(lat2) && Number.isFinite(lng2)) {
                    map.closePopup();
                    onTeleportRef.current(lat2, lng2);
                  }
                });
              });
            });
          } else {
            // Zoom to the level where this cluster breaks apart, centered on it.
            marker.on('click', () => {
              const target = Math.min(index.getClusterExpansionZoom(clusterId), 18);
              map.setView([lat, lng], target, { animate: true });
            });
          }
          marker.addTo(map);
          bookmarkMarkersRef.current.push(marker);
        }
      });
    };
    render();

    // Re-cull on every pan / zoom. moveend fires for both, so a single
    // listener covers what used to take a full O(n²) rebuild on zoomend only
    // (and never updated on pan at all).
    const onMove = () => render();
    map.on('moveend', onMove);
    return () => { map.off('moveend', onMove); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bookmarkPins, showBookmarkPins]);
}
