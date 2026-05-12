import React, { useState, useRef, useEffect, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { searchAddress } from '../services/api';
import { useT } from '../i18n';

interface SearchResult {
  name: string;
  lat: number;
  lng: number;
  address?: string;
}

interface AddressSearchProps {
  onSelect: (lat: number, lng: number, name: string) => void;
}

type Provider = 'nominatim' | 'photon' | 'google';

const AddressSearch: React.FC<AddressSearchProps> = ({ onSelect }) => {
  const t = useT();
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<SearchResult[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [showResults, setShowResults] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // Provider state — persisted in localStorage so it survives launches.
  // Auto-fallback to 'nominatim' if user previously picked 'google' but
  // never saved a key (or cleared it via DevTools).
  const [provider, setProvider] = useState<Provider>(() => {
    try {
      const saved = localStorage.getItem('locwarp.geocode_provider');
      const key = localStorage.getItem('locwarp.google_geocode_key') || '';
      if (saved === 'google' && key) return 'google';
      if (saved === 'photon') return 'photon';
    } catch { /* ignore */ }
    return 'nominatim';
  });
  const [googleKey, setGoogleKey] = useState<string>(() => {
    try { return localStorage.getItem('locwarp.google_geocode_key') || ''; }
    catch { return ''; }
  });
  const [showSettings, setShowSettings] = useState(false);
  const [keyInput, setKeyInput] = useState<string>(googleKey);

  const persistProvider = useCallback((p: Provider) => {
    setProvider(p);
    try { localStorage.setItem('locwarp.geocode_provider', p); }
    catch { /* ignore */ }
  }, []);

  const saveGoogleKey = useCallback(() => {
    const trimmed = keyInput.trim();
    if (!trimmed) return;
    setGoogleKey(trimmed);
    try { localStorage.setItem('locwarp.google_geocode_key', trimmed); }
    catch { /* ignore */ }
    persistProvider('google');
    setShowSettings(false);
  }, [keyInput, persistProvider]);

  const clearGoogleKey = useCallback(() => {
    setGoogleKey('');
    setKeyInput('');
    try { localStorage.removeItem('locwarp.google_geocode_key'); }
    catch { /* ignore */ }
    persistProvider('nominatim');
  }, [persistProvider]);

  const doSearch = useCallback(async (q: string) => {
    if (q.trim().length < 2) {
      setResults([]);
      setShowResults(false);
      setSearchError(null);
      return;
    }

    setIsLoading(true);
    setSearchError(null);
    try {
      const raw = await searchAddress(q);
      const mapped = (Array.isArray(raw) ? raw : []).map((r: any) => ({
        name: r.display_name || r.name || '',
        lat: r.lat,
        lng: r.lng,
        address: r.address || '',
      }));
      setResults(mapped);
      setShowResults(true);
    } catch (err: any) {
      console.error('Search failed:', err);
      setResults([]);
      const msg = err?.message || String(err);
      setSearchError(msg.length > 200 ? msg.slice(0, 200) + '…' : msg);
      setShowResults(true);
    } finally {
      setIsLoading(false);
    }
  }, []);

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => doSearch(value), 300);
  };

  const handleSelect = (result: SearchResult) => {
    setQuery(result.name);
    setShowResults(false);
    onSelect(result.lat, result.lng, result.name);
  };

  // Close result dropdown on outside click (settings is a modal — its own
  // backdrop handles dismissal so we don't need to do it here).
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (containerRef.current && !containerRef.current.contains(target)) {
        setShowResults(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const isGoogleActive = provider === 'google' && !!googleKey;
  const isPhotonActive = provider === 'photon';
  // Pill style + label for the three active providers. Free-tier ones
  // (Nominatim, Photon) share a muted look; Google uses its brand blue
  // to make the "paid key in use" state visible at a glance.
  const pillLabel = isGoogleActive
    ? 'Google'
    : isPhotonActive
      ? 'Photon'
      : 'Nominatim';
  const pillTitleKey = isGoogleActive
    ? 'search.settings_btn_google'
    : isPhotonActive
      ? 'search.settings_btn_photon'
      : 'search.settings_btn_free';

  return (
    <div ref={containerRef} style={{ position: 'relative' }}>
      <div style={{ position: 'relative', display: 'flex', gap: 4 }}>
        <div style={{ position: 'relative', flex: 1 }}>
          <input
            type="text"
            className="search-input"
            placeholder={t('search.placeholder')}
            value={query}
            onChange={handleInputChange}
            onFocus={() => { if (results.length > 0) setShowResults(true); }}
            style={{ width: '100%', paddingRight: 30 }}
          />
          <svg
            width="14" height="14" viewBox="0 0 24 24"
            fill="none" stroke="currentColor" strokeWidth="2"
            style={{
              position: 'absolute', right: 8, top: '50%',
              transform: 'translateY(-50%)', opacity: 0.4, pointerEvents: 'none',
            }}
          >
            <circle cx="11" cy="11" r="8" />
            <line x1="21" y1="21" x2="16.65" y2="16.65" />
          </svg>
        </div>
        <button
          onClick={() => { setKeyInput(googleKey); setShowSettings(true); }}
          title={t(pillTitleKey)}
          style={{
            display: 'flex', alignItems: 'center', gap: 4,
            padding: '0 10px',
            background: isGoogleActive ? 'rgba(66, 133, 244, 0.18)' : 'rgba(255, 255, 255, 0.06)',
            border: `1px solid ${isGoogleActive ? 'rgba(66, 133, 244, 0.55)' : 'rgba(255, 255, 255, 0.16)'}`,
            borderRadius: 4,
            color: isGoogleActive ? '#9ac0ff' : '#c8cad2',
            fontSize: 11,
            fontWeight: 600,
            cursor: 'pointer',
            whiteSpace: 'nowrap',
            height: 28,
          }}
        >
          <span
            style={{
              width: 6, height: 6, borderRadius: '50%',
              background: isGoogleActive ? '#4285f4' : '#9499ac',
              boxShadow: isGoogleActive ? '0 0 6px rgba(66, 133, 244, 0.8)' : 'none',
            }}
          />
          {pillLabel}
        </button>
      </div>

      {/* Settings modal — rendered via portal to avoid being clipped by the
          search container's narrow width. The user complained the inline
          dropdown squeezed the text; this gives it a comfortable 480px
          width with proper line-height and dedicated sections. */}
      {showSettings && createPortal(
        <div
          onClick={(e) => { if (e.target === e.currentTarget) setShowSettings(false); }}
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9000,
            background: 'rgba(8, 11, 22, 0.6)',
            backdropFilter: 'blur(4px)',
            WebkitBackdropFilter: 'blur(4px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
        >
          <div
            className="anim-fade-slide-up"
            style={{
              width: 480,
              maxWidth: '100%',
              maxHeight: 'calc(100vh - 60px)',
              overflowY: 'auto',
              background: 'rgba(26, 29, 39, 0.97)',
              backdropFilter: 'blur(16px) saturate(160%)',
              WebkitBackdropFilter: 'blur(16px) saturate(160%)',
              border: '1px solid rgba(108, 140, 255, 0.32)',
              borderRadius: 12,
              padding: '20px 22px',
              color: '#e8eaf0',
              boxShadow: '0 24px 60px rgba(8, 11, 22, 0.7)',
            }}
          >
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              marginBottom: 14,
            }}>
              <div style={{ fontSize: 15, fontWeight: 700, color: '#9ac0ff' }}>
                {t('search.settings_title')}
              </div>
              <button
                onClick={() => setShowSettings(false)}
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#9499ac',
                  fontSize: 20,
                  lineHeight: 1,
                  padding: '4px 8px',
                  cursor: 'pointer',
                  borderRadius: 4,
                }}
                aria-label="close"
              >×</button>
            </div>

            {/* Free / Nominatim option */}
            <label
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 10,
                padding: '12px 14px',
                borderRadius: 8,
                cursor: 'pointer',
                background: provider === 'nominatim' ? 'rgba(108, 140, 255, 0.10)' : 'rgba(255, 255, 255, 0.02)',
                border: `1px solid ${provider === 'nominatim' ? 'rgba(108, 140, 255, 0.32)' : 'rgba(255, 255, 255, 0.06)'}`,
                marginBottom: 10,
                transition: 'all 0.16s',
              }}
              onClick={() => persistProvider('nominatim')}
            >
              <input
                type="radio"
                name="geocode-provider-modal"
                checked={provider === 'nominatim'}
                onChange={() => persistProvider('nominatim')}
                style={{ marginTop: 3, accentColor: '#6c8cff' }}
              />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 700, fontSize: 13.5 }}>
                  {t('search.provider_free_label')}
                </div>
                <div style={{ fontSize: 12, opacity: 0.72, marginTop: 4, lineHeight: 1.55 }}>
                  {t('search.provider_free_desc')}
                </div>
              </div>
            </label>

            {/* Photon option — second free / no-key choice. Same shape as
                Nominatim row above, just a different OSM mirror so users
                can A/B which one happens to know their address better. */}
            <label
              style={{
                display: 'flex', alignItems: 'flex-start', gap: 10,
                padding: '12px 14px',
                borderRadius: 8,
                cursor: 'pointer',
                background: provider === 'photon' ? 'rgba(108, 140, 255, 0.10)' : 'rgba(255, 255, 255, 0.02)',
                border: `1px solid ${provider === 'photon' ? 'rgba(108, 140, 255, 0.32)' : 'rgba(255, 255, 255, 0.06)'}`,
                marginBottom: 10,
                transition: 'all 0.16s',
              }}
              onClick={() => persistProvider('photon')}
            >
              <input
                type="radio"
                name="geocode-provider-modal"
                checked={provider === 'photon'}
                onChange={() => persistProvider('photon')}
                style={{ marginTop: 3, accentColor: '#6c8cff' }}
              />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 700, fontSize: 13.5 }}>
                  {t('search.provider_photon_label')}
                </div>
                <div style={{ fontSize: 12, opacity: 0.72, marginTop: 4, lineHeight: 1.55 }}>
                  {t('search.provider_photon_desc')}
                </div>
              </div>
            </label>

            {/* Google option */}
            <div
              style={{
                padding: '12px 14px',
                borderRadius: 8,
                background: provider === 'google' ? 'rgba(66, 133, 244, 0.10)' : 'rgba(255, 255, 255, 0.02)',
                border: `1px solid ${provider === 'google' ? 'rgba(66, 133, 244, 0.36)' : 'rgba(255, 255, 255, 0.06)'}`,
                transition: 'all 0.16s',
              }}
            >
              <label
                style={{ display: 'flex', alignItems: 'flex-start', gap: 10, cursor: googleKey ? 'pointer' : 'default' }}
                onClick={() => { if (googleKey) persistProvider('google'); }}
              >
                <input
                  type="radio"
                  name="geocode-provider-modal"
                  checked={provider === 'google'}
                  onChange={() => { if (googleKey) persistProvider('google'); }}
                  disabled={!googleKey}
                  style={{ marginTop: 3, accentColor: '#6c8cff' }}
                />
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 700, fontSize: 13.5 }}>
                    {t('search.provider_google_label')}
                  </div>
                  <div style={{ fontSize: 12, opacity: 0.72, marginTop: 4, lineHeight: 1.55 }}>
                    {t('search.provider_google_desc')}
                    <span style={{ marginLeft: 6, opacity: 0.9 }}>
                      ·{' '}
                      <a
                        href="https://console.cloud.google.com/apis/library/geocoding-backend.googleapis.com"
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={(e) => e.stopPropagation()}
                        style={{ color: '#9ac0ff', textDecoration: 'underline' }}
                      >
                        {t('search.signup_label')}
                      </a>
                    </span>
                  </div>
                  <div style={{
                    fontSize: 11, color: 'var(--accent-green)',
                    marginTop: 6, display: 'flex', alignItems: 'center', gap: 6,
                  }}>
                    <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4">
                      <path d="M12 2l3 7h7l-5.5 4.5L18 21l-6-4-6 4 1.5-7.5L2 9h7z" />
                    </svg>
                    {t('search.privacy_hint')}
                  </div>
                </div>
              </label>

              {/* Quota warning — proper line height, lots of room */}
              <div style={{
                fontSize: 12, color: '#ffba6b',
                marginTop: 12, padding: '10px 12px',
                background: 'rgba(255, 186, 107, 0.08)',
                border: '1px solid rgba(255, 186, 107, 0.24)',
                borderRadius: 6,
                lineHeight: 1.6,
                display: 'flex', alignItems: 'flex-start', gap: 8,
              }}>
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" style={{ flexShrink: 0, marginTop: 2 }}>
                  <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/>
                  <line x1="12" y1="9" x2="12" y2="13"/>
                  <line x1="12" y1="17" x2="12" y2="17"/>
                </svg>
                <span>{t('search.provider_google_quota_hint')}</span>
              </div>

              {/* Key input + save row */}
              <div style={{ marginTop: 14 }}>
                <div style={{ fontSize: 11, color: '#9499ac', marginBottom: 6, fontWeight: 600 }}>
                  {t('search.api_key_label')}
                </div>
                <div style={{ display: 'flex', gap: 8 }}>
                  <input
                    type="password"
                    value={keyInput}
                    onChange={(e) => setKeyInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') { e.preventDefault(); saveGoogleKey(); }
                    }}
                    placeholder="AIza..."
                    spellCheck={false}
                    autoComplete="off"
                    style={{
                      flex: 1,
                      padding: '8px 10px',
                      fontSize: 13,
                      fontFamily: 'monospace',
                      background: 'rgba(12, 15, 26, 0.85)',
                      color: '#e8eaf0',
                      border: '1px solid rgba(108, 140, 255, 0.35)',
                      borderRadius: 6,
                      outline: 'none',
                    }}
                  />
                  <button
                    onClick={saveGoogleKey}
                    disabled={!keyInput.trim()}
                    className="action-btn primary"
                    style={{ fontSize: 12, padding: '0 16px' }}
                  >
                    {t('search.save_key')}
                  </button>
                </div>
                {googleKey && (
                  <div style={{
                    marginTop: 8, display: 'flex',
                    justifyContent: 'space-between', alignItems: 'center',
                  }}>
                    <span style={{ fontSize: 11, opacity: 0.55 }}>
                      {t('search.key_saved_hint', { tail: googleKey.slice(-4) })}
                    </span>
                    <button
                      onClick={clearGoogleKey}
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: '#ff8a8a',
                        cursor: 'pointer',
                        fontSize: 11,
                        padding: 0,
                        textDecoration: 'underline',
                      }}
                    >
                      {t('search.clear_key')}
                    </button>
                  </div>
                )}
              </div>
            </div>

            <div style={{ marginTop: 18, textAlign: 'right' }}>
              <button
                onClick={() => setShowSettings(false)}
                className="action-btn"
                style={{ fontSize: 12, padding: '6px 18px' }}
              >
                {t('generic.confirm')}
              </button>
            </div>
          </div>
        </div>,
        document.body,
      )}

      {isLoading && (
        <div style={{ fontSize: 11, opacity: 0.5, padding: '4px 0' }}>{t('search.searching')}</div>
      )}

      {showResults && results.length > 0 && (
        <div
          className="search-results"
          style={{
            position: 'absolute',
            top: '100%', left: 0, right: 0,
            background: '#2a2a2e', color: '#e8eaf0',
            border: '1px solid #444', borderRadius: 4,
            marginTop: 4, maxHeight: 240, overflowY: 'auto',
            zIndex: 200, boxShadow: '0 4px 12px rgba(0,0,0,0.4)',
          }}
        >
          {results.map((result, idx) => (
            <div
              key={idx}
              className="search-result-item"
              style={{
                padding: '8px 12px', cursor: 'pointer',
                borderBottom: idx < results.length - 1 ? '1px solid #333' : 'none',
                fontSize: 13, transition: 'background 0.15s',
              }}
              onClick={() => handleSelect(result)}
              onMouseEnter={(e) => { (e.currentTarget as HTMLDivElement).style.background = '#3a3a3e'; }}
              onMouseLeave={(e) => { (e.currentTarget as HTMLDivElement).style.background = 'transparent'; }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <svg
                  width="12" height="12" viewBox="0 0 24 24"
                  fill="none" stroke="currentColor" strokeWidth="2"
                  style={{ flexShrink: 0, opacity: 0.5 }}
                >
                  <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0118 0z" />
                  <circle cx="12" cy="10" r="3" />
                </svg>
                <div style={{ minWidth: 0 }}>
                  <div style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                    {result.name}
                  </div>
                  {result.address && (
                    <div style={{ fontSize: 10, opacity: 0.5, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {result.address}
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {showResults && !isLoading && results.length === 0 && query.trim().length >= 2 && (
        <div
          style={{
            position: 'absolute',
            top: '100%', left: 0, right: 0,
            background: '#2a2a2e',
            border: '1px solid #444', borderRadius: 4,
            marginTop: 4, padding: '12px',
            fontSize: 12, opacity: searchError ? 0.85 : 0.6,
            textAlign: 'center', zIndex: 200,
            color: searchError ? '#ff8a8a' : '#e8eaf0',
          }}
        >
          {searchError || t('search.no_results')}
        </div>
      )}
    </div>
  );
};

export default AddressSearch;
