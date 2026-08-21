import React, { useEffect, useState, useCallback, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';
import { API_BASE as API } from '../services/api';

interface PhoneNic {
  ip: string;
  iface: string;
  kind: 'wifi' | 'ethernet' | 'virtual' | 'other';
  primary: boolean;
}

export interface PhoneInfo {
  port: number;
  lan_ips: string[];
  nics?: PhoneNic[];
  token: string;
  last_phone_hit_ago_s?: number | null;
}

/** Build the capability URL that the phone opens to control LocWarp. */
export function buildPhoneControlUrl(
  info: Pick<PhoneInfo, 'port' | 'token'> | null | undefined,
  ip: string | null | undefined,
): string {
  const token = typeof info?.token === 'string' ? info.token.trim() : '';
  const host = typeof ip === 'string' ? ip.trim() : '';
  const port = info?.port;
  if (!token || !host || typeof port !== 'number' || !Number.isFinite(port) || port <= 0) return '';
  return `http://${host}:${port}/phone#t=${token}`;
}

interface PhoneControlButtonProps {
  showToast?: (msg: string) => void;
}

const PhoneControlButton: React.FC<PhoneControlButtonProps> = ({ showToast }) => {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [info, setInfo] = useState<PhoneInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selectedIp, setSelectedIp] = useState<string | null>(null);
  const infoRequestSeq = useRef(0);
  const rotateInFlight = useRef(false);

  const fetchInfo = useCallback(async (options: { allowDuringRotate?: boolean } = {}): Promise<PhoneInfo | null> => {
    // Polling and selection changes must not start another read while rotate
    // is replacing the capability. The explicit post-rotate refresh opts in
    // below so the new token is fetched before rotate reports success.
    if (rotateInFlight.current && !options.allowDuringRotate) return null;
    const requestSeq = ++infoRequestSeq.current;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API}/api/phone/info`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: PhoneInfo = await r.json();
      // A response that started before a rotate (or a newer refresh) no
      // longer owns the displayed info. This also protects against fetch
      // implementations that ignore AbortSignal in tests or older browsers.
      if (requestSeq !== infoRequestSeq.current) return null;
      setInfo(j);
      if (!selectedIp || !j.lan_ips.includes(selectedIp)) {
        setSelectedIp(j.lan_ips[0] ?? null);
      }
      return j;
    } catch (e: any) {
      if (requestSeq === infoRequestSeq.current) setErr(e?.message ?? 'failed');
      throw e;
    } finally {
      if (requestSeq === infoRequestSeq.current) setLoading(false);
    }
  }, [selectedIp]);

  useEffect(() => {
    if (open) void fetchInfo().catch(() => undefined);
  }, [open, fetchInfo]);

  const rotate = useCallback(async () => {
    if (rotateInFlight.current) return;
    rotateInFlight.current = true;
    // Transfer ownership away from every info request already in flight.
    // Their late responses can finish normally, but cannot overwrite the
    // freshly rotated URL or its selected NIC.
    infoRequestSeq.current += 1;
    setLoading(true);
    setErr(null);
    try {
      const r = await fetch(`${API}/api/phone/rotate`, { method: 'POST' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      // The POST invalidates the old capability immediately. Remove it from
      // the UI before waiting for the replacement info request, so a failed
      // refresh can never leave an expired URL visible or copyable.
      setInfo(null);
      // Do not claim success until the desktop has observed the new token.
      // Keeping rotateInFlight set gates the background poll during this read.
      await fetchInfo({ allowDuringRotate: true });
      showToast?.(t('phone.rotated'));
    } catch (e: any) {
      setErr(e?.message ?? 'failed');
    } finally {
      rotateInFlight.current = false;
      setLoading(false);
    }
  }, [fetchInfo, showToast, t]);

  const copy = useCallback(async (s: string) => {
    try {
      await navigator.clipboard.writeText(s);
      showToast?.(t('phone.copied'));
    } catch { /* ignore */ }
  }, [showToast, t]);

  const url = buildPhoneControlUrl(info, selectedIp);

  const [fwState, setFwState] = useState<'idle' | 'busy' | 'ok' | 'fail'>('idle');
  const [fwMsg, setFwMsg] = useState<string>('');
  const repairFirewall = useCallback(async () => {
    setFwState('busy');
    setFwMsg('');
    try {
      const r = await fetch(`${API}/api/phone/firewall_repair`, { method: 'POST' });
      const j = await r.json();
      if (j.ok) {
        setFwState('ok');
        setFwMsg(t('phone.firewall_repair_ok'));
        showToast?.(t('phone.firewall_repair_ok'));
      } else {
        setFwState('fail');
        setFwMsg(j.message || t('phone.firewall_repair_failed'));
      }
    } catch (e: any) {
      setFwState('fail');
      setFwMsg(e?.message || t('phone.firewall_repair_failed'));
    }
    setTimeout(() => setFwState('idle'), 4500);
  }, [showToast, t]);

  // Poll while modal open so the "phone reached the URL" indicator lights
  // up live when the user actually opens the link on their phone.
  useEffect(() => {
    if (!open) return;
    const id = setInterval(() => { void fetchInfo().catch(() => undefined); }, 2000);
    return () => clearInterval(id);
  }, [open, fetchInfo]);

  const reachAgo = info?.last_phone_hit_ago_s;
  const reachOk = typeof reachAgo === 'number' && reachAgo < 60;

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
                    <div style={{ fontSize: 11, color: '#97a0b3', marginBottom: 4 }}>
                      {t('phone.lan_url')}
                    </div>
                    {(info.nics && info.nics.filter((n) => n.kind !== 'virtual').length > 1) ? (
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
                        {info.nics.filter((n) => n.kind !== 'virtual').map((n) => (
                          <option key={n.ip} value={n.ip}>
                            {n.ip} {n.iface ? `— ${n.iface}` : ''} {n.kind === 'wifi' ? '(Wi-Fi)' : n.kind === 'ethernet' ? '(Ethernet)' : ''} {n.primary ? '★' : ''}
                          </option>
                        ))}
                      </select>
                    ) : info.lan_ips.length > 1 && (
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
                      {url || t('phone.no_url')}
                    </div>
                    <div style={{
                      marginTop: 8, padding: '7px 10px', borderRadius: 6,
                      background: 'rgba(255, 180, 60, 0.10)',
                      border: '1px solid rgba(255, 180, 60, 0.32)',
                      color: '#ffc870', fontSize: 11, lineHeight: 1.45,
                    }}>
                      {t('phone.url_warning')}
                    </div>
                  </div>

                  <div style={{
                    fontSize: 11, padding: '6px 10px', borderRadius: 6,
                    border: `1px solid ${reachOk ? 'rgba(77, 210, 138, 0.5)' : 'rgba(255,255,255,0.10)'}`,
                    background: reachOk ? 'rgba(77, 210, 138, 0.12)' : 'rgba(255,255,255,0.04)',
                    color: reachOk ? '#7ee2a4' : '#97a0b3',
                  }}>
                    {reachOk
                      ? t('phone.reach_ok', { sec: String(Math.max(0, Math.round(reachAgo as number))) })
                      : t('phone.reach_unknown')}
                  </div>

                </div>

                <div style={{ display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4, flex: 1, minWidth: 0 }}>
                    <button
                      onClick={repairFirewall}
                      disabled={fwState === 'busy'}
                      title={t('phone.firewall_repair_tooltip')}
                      style={{
                        padding: '6px 10px', fontSize: 11,
                        background:
                          fwState === 'ok' ? 'rgba(77, 210, 138, 0.18)' :
                          fwState === 'fail' ? 'rgba(239, 93, 93, 0.18)' :
                          fwState === 'busy' ? 'rgba(108, 140, 255, 0.15)' :
                          'rgba(255, 180, 60, 0.12)',
                        border: `1px solid ${
                          fwState === 'ok' ? 'rgba(77, 210, 138, 0.55)' :
                          fwState === 'fail' ? 'rgba(239, 93, 93, 0.55)' :
                          fwState === 'busy' ? 'rgba(108, 140, 255, 0.45)' :
                          'rgba(255, 180, 60, 0.45)'}`,
                        color:
                          fwState === 'ok' ? '#7ee2a4' :
                          fwState === 'fail' ? '#ef9999' :
                          fwState === 'busy' ? '#9bb0ff' :
                          '#ffc870',
                        borderRadius: 4, cursor: fwState === 'busy' ? 'wait' : 'pointer',
                        display: 'inline-flex', alignItems: 'center', gap: 5,
                        alignSelf: 'flex-start',
                      }}
                    >
                      {fwState === 'busy' ? (
                        <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                          <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83" />
                        </svg>
                      ) : fwState === 'ok' ? (
                        <span style={{ fontSize: 13, fontWeight: 700 }}>✓</span>
                      ) : fwState === 'fail' ? (
                        <span style={{ fontSize: 13, fontWeight: 700 }}>✗</span>
                      ) : (
                        <span style={{ fontSize: 13, fontWeight: 700 }}>!</span>
                      )}
                      <span>
                        {fwState === 'busy' ? t('phone.firewall_repair_busy') :
                         fwState === 'ok' ? t('phone.firewall_repair_ok') :
                         fwState === 'fail' ? t('phone.firewall_repair_failed') :
                         t('phone.firewall_repair_button')}
                      </span>
                    </button>
                    {fwMsg && fwState !== 'idle' && (
                      <div style={{
                        fontSize: 10, lineHeight: 1.4,
                        color: fwState === 'fail' ? '#ef9999' : '#7ee2a4',
                        wordBreak: 'break-word',
                      }}>
                        {fwMsg}
                      </div>
                    )}
                  </div>

                  <div style={{ display: 'flex', gap: 8 }}>
                  <button
                    onClick={rotate}
                    disabled={loading}
                    title={t('phone.rotate_tooltip')}
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
