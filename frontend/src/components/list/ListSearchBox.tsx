import React from 'react';
import { useT } from '../../i18n';

interface ListSearchBoxProps {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  // Extra style merged onto the relative wrapper (e.g. marginBottom).
  style?: React.CSSProperties;
}

// Search input with a magnifier icon and a clear (×) button.
export function ListSearchBox({ value, onChange, placeholder, style }: ListSearchBoxProps) {
  const t = useT();
  return (
    <div style={{ position: 'relative', ...style }}>
      <svg
        width="12" height="12" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" strokeWidth="2"
        style={{ position: 'absolute', left: 8, top: '50%', transform: 'translateY(-50%)', opacity: 0.4, pointerEvents: 'none' }}
      >
        <circle cx="11" cy="11" r="8" />
        <line x1="21" y1="21" x2="16.65" y2="16.65" />
      </svg>
      <input
        type="text"
        className="search-input"
        placeholder={placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: '100%', paddingLeft: 26, paddingRight: value ? 24 : 8, fontSize: 12 }}
      />
      {value && (
        <button
          onClick={() => onChange('')}
          title={t('bm.search_clear')}
          style={{
            position: 'absolute', right: 4, top: '50%', transform: 'translateY(-50%)',
            background: 'none', border: 'none', color: '#bbb',
            cursor: 'pointer', padding: '2px 6px', fontSize: 14, lineHeight: 1,
          }}
        >×</button>
      )}
    </div>
  );
}
