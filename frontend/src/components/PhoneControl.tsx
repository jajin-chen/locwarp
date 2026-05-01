import React, { useEffect, useState, useCallback } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';

interface PhoneInfo {
  port: number;
  lan_ips: string[];
  pin: string;
}

interface PhoneControlButtonProps {
  showToast?: (msg: string) => void;
}

const API = 'http://127.0.0.1:8777';

const PhoneControlButton: React.FC<PhoneControlButtonProps> = ({ showToast }) => {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<PhoneInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selectedIp, setSelectedIp] = useState<string | null>(null);

  const fetchInfo = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API}/api/phone/info`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: PhoneInfo = await r.json();
      setInfo(j);
      if (!selectedIp || !j.lan_ips.includes(selectedIp)) {
        setSelectedIp(j.lan_ips[0] ?? null);
      }
    } catch (e: any) {
      setErr(e?.message ?? 'failed');
    } finally {
      setLoading(false);
    }
  }, [selectedIp]);

  useEffect(() => {
    if (open) fetchInfo();
  }, [open, fetchInfo]);

  const rotate = useCallback(async () => {
    setLoading(true);
    try {
      const r = await fetch(`${API}/api/phone/rotate`, { method: 'POST' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await fetchInfo();
      showToast?.(t('phone.rotated'));
    } catch (e: any) {
      setErr(e?.message ?? 'failed');
    } finally {
      setLoading(false);
    }
  }, [fetchInfo, showToast, t]);

  const copy = useCallback(async (s: string) => {
    try {
      await navigator.clipboard.writeText(s);
      showToast?.(t('phone.copied'));
    } catch { /* ignore */ }
  }, [showToast, t]);

  const url = info && selectedIp ? `http://${selectedIp}:${info.port}/phone` : '';

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        title={t('phone.tooltip')}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          padding: '2px 8px',
          fontSize: 12,
          background: 'rgba(77, 210, 138, 0.12)',
          border: '1px solid rgba(77, 210, 138, 0.4)',
          color: '#4dd28a',
          borderRadius: 4,
          cursor: 'pointer',
        }}
      >
        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <rect x="6" y="2" width="12" height="20" rx="2" />
          <line x1="11" y1="18" x2="13" y2="18" />
        </svg>
        {t('phone.button')}
      </button>

      {open && createPortal((
        <div
          onClick={() => setOpen(false)}
          style={{
            position: 'fixed', inset: 0, zIndex: 2000,
            background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              width: 420, maxWidth: 'calc(100vw - 32px)',
              background: 'rgba(26, 29, 39, 0.98)',
              border: '1px solid rgba(108, 140, 255, 0.3)',
              borderRadius: 12,
              padding: 22,
              boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
              color: '#e6e8ee',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
              <h2 style={{ margin: 0, fontSize: 16 }}>{t('phone.modal_title')}</h2>
              <button
                onClick={() => setOpen(false)}
                style={{
                  background: 'transparent', border: 'none', color: '#97a0b3',
                  cursor: 'pointer', fontSize: 18, padding: '0 4px',
                }}
              >×</button>
            </div>

            <div style={{ fontSize: 12, color: '#97a0b3', lineHeight: 1.6, marginBottom: 14 }}>
              {t('phone.help')}
            </div>

            {loading && !info && <div style={{ fontSize: 13 }}>{t('generic.loading')}…</div>}
            {err && <div style={{ color: '#ef5d5d', fontSize: 13 }}>{err}</div>}

            {info && (
              <>
                <div style={{ display: 'flex', flexDirection: 'column', gap: 14, marginBottom: 16 }}>
                  <div>
                    <div style={{ fontSize: 11, color: '#97a0b3', marginBottom: 4 }}>{t('phone.lan_url')}</div>
                    {info.lan_ips.length > 1 && (
                      <select
                        value={selectedIp ?? ''}
                        onChange={(e) => setSelectedIp(e.target.value)}
                        style={{
                          width: '100%', marginBottom: 6, padding: '4px 6px',
                          background: '#0f1218', color: '#e6e8ee',
                          border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6,
                          fontSize: 12,
                        }}
                      >
                        {info.lan_ips.map((ip) => (
                          <option key={ip} value={ip}>{ip}</option>
                        ))}
                      </select>
                    )}
                    <div
                      onClick={() => url && copy(url)}
                      style={{
                        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                        fontSize: 14, color: '#6c8cff',
                        background: '#0f1218', padding: '10px 12px', borderRadius: 8,
                        cursor: url ? 'pointer' : 'default', wordBreak: 'break-all',
                        border: '1px solid rgba(255,255,255,0.08)',
                        textAlign: 'center', fontWeight: 500,
                      }}
                      title={t('phone.copy_url')}
                    >
                      {url || t('phone.no_lan')}
                    </div>
                  </div>

                  <div>
                    <div style={{ fontSize: 11, color: '#97a0b3', marginBottom: 4 }}>PIN</div>
                    <div
                      onClick={() => copy(info.pin)}
                      style={{
                        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                        fontSize: 30, letterSpacing: 8, fontWeight: 600,
                        background: '#0f1218', padding: '14px 12px', borderRadius: 8,
                        textAlign: 'center', cursor: 'pointer',
                        border: '1px solid rgba(255,255,255,0.08)',
                      }}
                      title={t('phone.copy_pin')}
                    >
                      {info.pin}
                    </div>
                  </div>
                </div>

                <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
                  <button
                    onClick={rotate}
                    disabled={loading}
                    style={{
                      padding: '6px 12px', fontSize: 12,
                      background: 'rgba(239, 93, 93, 0.12)',
                      border: '1px solid rgba(239, 93, 93, 0.4)',
                      color: '#ef5d5d', borderRadius: 4, cursor: 'pointer',
                    }}
                  >
                    {t('phone.rotate')}
                  </button>
                  <button
                    onClick={() => setOpen(false)}
                    style={{
                      padding: '6px 12px', fontSize: 12,
                      background: 'rgba(108, 140, 255, 0.18)',
                      border: '1px solid rgba(108, 140, 255, 0.4)',
                      color: '#6c8cff', borderRadius: 4, cursor: 'pointer',
                    }}
                  >
                    {t('generic.close')}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      ), document.body)}
    </>
  );
};

export default PhoneControlButton;
