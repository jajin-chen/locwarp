import React from 'react';
import { useT } from '../../i18n';

interface SortModeSelectProps<M extends string> {
  value: M;
  onChange: (m: M) => void;
  options: { value: M; label: string }[];
  // Extra style merged onto the wrapper row (e.g. marginTop / marginBottom).
  style?: React.CSSProperties;
}

// Sort control — choose how a list is ordered.
export function SortModeSelect<M extends string>({ value, onChange, options, style }: SortModeSelectProps<M>) {
  const t = useT();
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: '#bbb', ...style }}>
      <span style={{ opacity: 0.7 }}>{t('bm.sort_label')}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value as M)}
        style={{
          flex: 1, background: '#1e1e22', color: '#e0e0e0',
          border: '1px solid rgba(255,255,255,0.12)', borderRadius: 4,
          padding: '3px 6px', fontSize: 11,
        }}
      >
        {/* Explicit inline colors so the popup list is readable on
            Windows native select dropdown (which defaults to white bg). */}
        {options.map((opt) => (
          <option key={opt.value} value={opt.value} style={{ background: '#1e1e22', color: '#e0e0e0' }}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}
