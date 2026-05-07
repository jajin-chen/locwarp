import React, { useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';
import {
  isAlertSoundEnabled,
  setAlertSoundEnabled,
  playCompletionAlert,
} from '../services/alertSound';

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * General-purpose settings panel. Currently hosts a single toggle (route
 * completion alert sound) but is structured as a sectioned layout so
 * future settings (e.g. default speed presets, map tile choice, dual-
 * device sync prefs) can drop into new <section> blocks without
 * restructuring.
 */
const SettingsModal: React.FC<Props> = ({ open, onClose }) => {
  const t = useT();
  // Lazily snapshot the current persisted value the first time the modal
  // mounts. Subsequent toggles update both the React state (for immediate
  // UI feedback) and localStorage (for persistence + cross-tab read by
  // playCompletionAlert).
  const [alertEnabled, setAlertEnabledLocal] = useState<boolean>(() => isAlertSoundEnabled());

  if (!open) return null;

  const handleToggle = (next: boolean) => {
    setAlertEnabledLocal(next);
    setAlertSoundEnabled(next);
  };

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    padding: '8px 0',
  };
  const rowLabelStyle: React.CSSProperties = {
    fontSize: 13,
    color: '#e8eaf0',
    cursor: 'pointer',
    userSelect: 'none',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 8,
  };
  // Small "(?)" help marker rendered next to a setting label. Hover
  // surfaces the description via the native title attribute, keeping the
  // setting row visually compact while still reachable for users who
  // want the explanation.
  const helpIconStyle: React.CSSProperties = {
    width: 14, height: 14,
    display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
    fontSize: 9, fontWeight: 700,
    color: 'rgba(255,255,255,0.55)',
    background: 'rgba(255,255,255,0.06)',
    border: '1px solid rgba(255,255,255,0.12)',
    borderRadius: '50%',
    cursor: 'help',
    fontFamily: 'ui-monospace, monospace',
    lineHeight: 1,
    userSelect: 'none',
  };

  return createPortal((
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 2000,
        background: 'rgba(8, 10, 20, 0.55)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 560, maxWidth: 'calc(100vw - 48px)',
          maxHeight: 'calc(100vh - 80px)',
          position: 'relative',
          background: 'rgba(26, 29, 39, 0.96)',
          border: '1px solid rgba(108, 140, 255, 0.25)', borderRadius: 14,
          padding: '28px 32px', color: '#e8eaf0',
          boxShadow: '0 20px 60px rgba(12, 18, 40, 0.65)',
          overflowY: 'auto',
        }}
      >
        <button
          onClick={onClose}
          title={t('generic.close')}
          aria-label={t('generic.close')}
          style={{
            position: 'absolute', top: 12, right: 12,
            width: 28, height: 28, borderRadius: 6,
            background: 'transparent', border: 'none',
            color: 'rgba(255,255,255,0.5)', cursor: 'pointer',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>

        <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 24, paddingRight: 32 }}>
          {t('settings.title')}
        </div>

        <div style={rowStyle}>
          <label style={rowLabelStyle}>
            <input
              type="checkbox"
              checked={alertEnabled}
              onChange={(e) => handleToggle(e.target.checked)}
              style={{ cursor: 'pointer', margin: 0 }}
            />
            <span>{t('settings.alert_sound_label')}</span>
            <span
              style={helpIconStyle}
              title={t('settings.alert_sound_desc')}
              aria-label={t('settings.alert_sound_desc')}
            >?</span>
          </label>
          <button
            onClick={() => playCompletionAlert(true)}
            style={{
              padding: '5px 12px', fontSize: 12,
              background: 'rgba(108, 140, 255, 0.15)',
              color: '#a8b8ff',
              border: '1px solid rgba(108, 140, 255, 0.4)',
              borderRadius: 6, cursor: 'pointer',
            }}
          >
            {t('settings.alert_sound_test')}
          </button>
        </div>
      </div>
    </div>
  ), document.body);
};

export default SettingsModal;
