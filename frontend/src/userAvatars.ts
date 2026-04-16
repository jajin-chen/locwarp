/**
 * User avatar presets + rendering helper for the map "current position"
 * marker.
 *
 * Two separate localStorage keys:
 *   - locwarp.user_avatar            → the active selection (type + presetId)
 *   - locwarp.user_avatar_custom_png → last-uploaded PNG as a DataURL
 *
 * We split them so picking a preset (or the default blue dot) does NOT
 * delete the user's uploaded PNG. User wanted: upload persists, next
 * upload overwrites, but switching the active selection never wipes it.
 */

import hareUrl from './assets/avatars/hare.png';
import dogUrl from './assets/avatars/dog.png';
import catUrl from './assets/avatars/cat.png';
import foxUrl from './assets/avatars/fox.png';
import boyUrl from './assets/avatars/boy.png';
import girlUrl from './assets/avatars/girl.png';

export type UserAvatar =
  | { type: 'default' }
  | { type: 'preset'; presetId: string }
  | { type: 'custom' };  // payload comes from loadCustomPng() so it survives switching

export const DEFAULT_AVATAR_HTML = `<svg width="44" height="44" viewBox="0 0 44 44" xmlns="http://www.w3.org/2000/svg">
  <defs>
    <radialGradient id="av-posGlow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#4285f4" stop-opacity="0.3"/>
      <stop offset="100%" stop-color="#4285f4" stop-opacity="0"/>
    </radialGradient>
    <filter id="av-posShadow" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="1" stdDeviation="2" flood-color="#4285f4" flood-opacity="0.6"/>
    </filter>
  </defs>
  <circle cx="22" cy="22" r="20" fill="url(#av-posGlow)"/>
  <circle cx="22" cy="22" r="11" fill="#4285f4" filter="url(#av-posShadow)"/>
  <circle cx="22" cy="22" r="9" fill="#2b6ff2"/>
  <circle cx="22" cy="18" r="3.5" fill="#ffffff" opacity="0.95"/>
  <path d="M15.5 28.5c0-3.6 2.9-6.5 6.5-6.5s6.5 2.9 6.5 6.5" fill="#ffffff" opacity="0.95" stroke="none"/>
  <circle cx="22" cy="22" r="11" fill="none" stroke="#ffffff" stroke-width="2" opacity="0.8"/>
</svg>`;

export interface Preset {
  id: string;
  label: string;
  url: string;  // Bundled PNG imported by Vite; stable hash-URL in build.
}

export const PRESETS: Preset[] = [
  { id: 'hare', label: '兔兔', url: hareUrl },
  { id: 'dog',  label: '小狗', url: dogUrl },
  { id: 'cat',  label: '小貓', url: catUrl },
  { id: 'fox',  label: '狐狸', url: foxUrl },
  { id: 'boy',  label: '男孩', url: boyUrl },
  { id: 'girl', label: '女孩', url: girlUrl },
];

/**
 * Resolve the HTML snippet to drop into the Leaflet divIcon. Needs the
 * current custom-PNG DataURL because `UserAvatar` intentionally doesn't
 * carry it inline (so switching to a preset doesn't drop the uploaded file).
 */
export function avatarToHtml(avatar: UserAvatar | null, customPng: string | null): string {
  if (!avatar || avatar.type === 'default') {
    return DEFAULT_AVATAR_HTML;
  }
  if (avatar.type === 'preset') {
    const p = PRESETS.find((x) => x.id === avatar.presetId);
    if (p) {
      return `<img src="${p.url}" width="44" height="44" style="display:block;width:44px;height:44px;object-fit:contain;" alt="" />`;
    }
    return DEFAULT_AVATAR_HTML;
  }
  if (avatar.type === 'custom' && customPng) {
    return `<img src="${customPng}" width="44" height="44" style="display:block;width:44px;height:44px;object-fit:contain;" alt="" />`;
  }
  return DEFAULT_AVATAR_HTML;
}

const AVATAR_KEY = 'locwarp.user_avatar';
const CUSTOM_PNG_KEY = 'locwarp.user_avatar_custom_png';

export function loadAvatar(): UserAvatar {
  try {
    const raw = localStorage.getItem(AVATAR_KEY);
    if (!raw) return { type: 'default' };
    const parsed = JSON.parse(raw);
    if (parsed?.type === 'default') return { type: 'default' };
    if (parsed?.type === 'preset' && typeof parsed.presetId === 'string') {
      return { type: 'preset', presetId: parsed.presetId };
    }
    if (parsed?.type === 'custom') {
      return { type: 'custom' };
    }
  } catch { /* ignore */ }
  return { type: 'default' };
}

export function saveAvatar(avatar: UserAvatar): void {
  try {
    // Don't serialize dataUrl here even if a caller sneaks it in — the
    // DataURL lives in its own slot so the PNG survives selection changes.
    const plain: UserAvatar = avatar.type === 'default'
      ? { type: 'default' }
      : avatar.type === 'preset'
        ? { type: 'preset', presetId: avatar.presetId }
        : { type: 'custom' };
    localStorage.setItem(AVATAR_KEY, JSON.stringify(plain));
  } catch { /* quota exceeded etc. */ }
}

export function loadCustomPng(): string | null {
  try {
    const raw = localStorage.getItem(CUSTOM_PNG_KEY);
    return raw && raw.startsWith('data:') ? raw : null;
  } catch {
    return null;
  }
}

export function saveCustomPng(dataUrl: string | null): void {
  try {
    if (dataUrl) localStorage.setItem(CUSTOM_PNG_KEY, dataUrl);
    else localStorage.removeItem(CUSTOM_PNG_KEY);
  } catch { /* ignore */ }
}

/**
 * Trim transparent borders and resize a PNG to keep localStorage tidy,
 * without changing the image's aspect ratio. Output PNG matches the
 * content's natural shape: square in → square out, wide in → wide out.
 * The longer edge is capped at `maxSize` (default 88, which renders
 * crisp at the 44-px map display). Transparency is preserved.
 *
 * Previous version also center-cropped wide/tall images to a square so
 * they'd fill the marker slot. That made landscape photos lose their
 * left/right edges, so we stopped forcing square. The caller should use
 * object-fit: contain to letterbox whatever shape comes out here.
 */
export function pngFileToDataUrl(file: File, maxSize = 88): Promise<string> {
  return new Promise((resolve, reject) => {
    if (file.type !== 'image/png') {
      reject(new Error('not-png'));
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('read-failed'));
    reader.onload = () => {
      const img = new Image();
      img.onerror = () => reject(new Error('decode-failed'));
      img.onload = () => {
        const iw = img.naturalWidth || img.width;
        const ih = img.naturalHeight || img.height;
        if (!iw || !ih) { reject(new Error('zero-size')); return; }

        // Draw full image to a staging canvas so we can inspect pixels.
        const stage = document.createElement('canvas');
        stage.width = iw; stage.height = ih;
        const sctx = stage.getContext('2d');
        if (!sctx) { reject(new Error('no-canvas')); return; }
        sctx.drawImage(img, 0, 0);

        // Alpha-trim: find bounding box of pixels with alpha > 8 so a
        // PNG with empty transparent padding snaps down to just the
        // subject. Photos without transparency will keep their full bounds.
        let left = iw, right = -1, top = ih, bottom = -1;
        try {
          const { data } = sctx.getImageData(0, 0, iw, ih);
          for (let y = 0; y < ih; y++) {
            for (let x = 0; x < iw; x++) {
              const alpha = data[(y * iw + x) * 4 + 3];
              if (alpha > 8) {
                if (x < left)   left = x;
                if (x > right)  right = x;
                if (y < top)    top = y;
                if (y > bottom) bottom = y;
              }
            }
          }
        } catch {
          left = 0; right = iw - 1; top = 0; bottom = ih - 1;
        }
        if (right < left || bottom < top) {
          left = 0; right = iw - 1; top = 0; bottom = ih - 1;
        }
        const cropW = right - left + 1;
        const cropH = bottom - top + 1;

        // Scale so the LONGER side matches maxSize, keeping aspect.
        const ratio = Math.min(maxSize / cropW, maxSize / cropH, 1);
        const outW = Math.max(1, Math.round(cropW * ratio));
        const outH = Math.max(1, Math.round(cropH * ratio));

        const canvas = document.createElement('canvas');
        canvas.width = outW;
        canvas.height = outH;
        const ctx = canvas.getContext('2d');
        if (!ctx) { reject(new Error('no-canvas')); return; }
        ctx.clearRect(0, 0, outW, outH);
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.drawImage(img, left, top, cropW, cropH, 0, 0, outW, outH);
        resolve(canvas.toDataURL('image/png'));
      };
      img.src = String(reader.result);
    };
    reader.readAsDataURL(file);
  });
}
