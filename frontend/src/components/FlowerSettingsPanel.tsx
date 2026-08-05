import React, { useMemo, useState } from 'react'
import { useT } from '../i18n'
import { useSimulation, SimMode, MoveMode } from '../hooks/useSimulation'

type Sim = ReturnType<typeof useSimulation>

// Number input that lets the user freely clear and retype the field (so a
// multi-digit value like 15 is easy to enter). The draft string is held
// locally while editing; the value is parsed and clamped via `set` only on
// blur / Enter, so per-keystroke clamping never fights the typing.
const NumberField: React.FC<{
  value: number
  set: (n: number) => void
  min: number
  max?: number
  step: number
}> = ({ value, set, min, max, step }) => {
  const [draft, setDraft] = useState<string | null>(null)
  const commit = () => {
    const n = parseFloat(draft ?? '')
    if (Number.isFinite(n)) set(n)
    setDraft(null)
  }
  return (
    <input
      type="number"
      className="lw-input"
      min={min}
      max={max}
      step={step}
      value={draft ?? String(value)}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') (e.target as HTMLInputElement).blur() }}
      style={{ width: 64 }}
    />
  )
}

// Compact H:MM:SS / M:SS duration formatter for the flower estimate.
const fmtDuration = (sec: number) => {
  const s = Math.max(0, Math.round(sec))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(ss)}` : `${m}:${pad(ss)}`
}

// 種花模式 settings block (teleport toggle, circle geometry / wait inputs,
// live total-time estimate). Rendered inside ControlPanel's waypoint editor
// section when the sim mode is Flower.
const FlowerSettingsPanel: React.FC<{ sim: Sim }> = ({ sim }) => {
  const t = useT()

  // 種花模式 estimated total trip time (seconds): every pre/post wait + the
  // walked approach between flowers (straight-line approximation; teleport
  // approaches cost no travel time) + the walked circle length, across all
  // rounds. Shown live in the panel so the user knows roughly how long the
  // whole run will take, circling included.
  const flowerEstimateSec = useMemo(() => {
    if (sim.mode !== SimMode.Flower) return null
    const wps = sim.waypoints
    if (wps.length < 1) return null
    const N = Math.max(3, Math.round(sim.flowerSegments))
    const R = sim.flowerRadius
    const circles = Math.max(0.5, Math.round(sim.flowerCircles * 2) / 2)
    const rounds = Math.max(1, Math.round(sim.flowerRounds))
    const preW = Math.max(0, sim.flowerPreWait)
    const postW = Math.max(0, sim.flowerPostWait)
    const defKmh = sim.moveMode === MoveMode.Running ? 19.8 : sim.moveMode === MoveMode.Driving ? 60 : 10.8
    const kmh = (sim.speedMinKmh != null && sim.speedMaxKmh != null)
      ? (sim.speedMinKmh + sim.speedMaxKmh) / 2
      : (sim.customSpeedKmh ?? defKmh)
    const speed = Math.max(kmh / 3.6, 0.1) // m/s
    // Walked length per flower circle: out to the first vertex (radius) plus
    // `circles` laps of the N-gon perimeter (matches backend _circle_path).
    const chord = 2 * R * Math.sin(Math.PI / N)
    const circleLen = R + circles * N * chord
    const hav = (a: { lat: number; lng: number }, b: { lat: number; lng: number }) => {
      const dlat = ((b.lat - a.lat) * Math.PI) / 180
      const dlng = ((b.lng - a.lng) * Math.PI) / 180 * Math.cos(((a.lat + b.lat) / 2) * Math.PI / 180)
      return 6371000 * Math.sqrt(dlat * dlat + dlng * dlng)
    }
    let total = 0
    let prev: { lat: number; lng: number } | null = sim.currentPosition
    for (let r = 0; r < rounds; r++) {
      for (let i = 0; i < wps.length; i++) {
        const wp = wps[i]
        total += preW
        if (!sim.flowerTeleport && prev) total += hav(prev, wp) / speed
        total += postW
        total += circleLen / speed
        prev = wp
      }
    }
    return total
  }, [sim.mode, sim.waypoints, sim.flowerSegments, sim.flowerRadius, sim.flowerCircles, sim.flowerRounds, sim.flowerPreWait, sim.flowerPostWait, sim.flowerTeleport, sim.customSpeedKmh, sim.speedMinKmh, sim.speedMaxKmh, sim.moveMode, sim.currentPosition])

  return (
    <div style={{
      marginBottom: 8, padding: '8px 10px',
      background: 'rgba(108, 140, 255, 0.06)',
      border: '1px solid rgba(108, 140, 255, 0.18)',
      borderRadius: 6, fontSize: 11,
    }}>
      <div style={{ opacity: 0.6, marginBottom: 8, lineHeight: 1.4 }}>{t('flower.hint')}</div>
      {/* teleport vs walk approach */}
      <label className="lw-checkbox" title={t('flower.teleport_hint')} style={{ marginBottom: 8 }}>
        <input
          type="checkbox"
          checked={sim.flowerTeleport}
          onChange={(e) => sim.setFlowerTeleport(e.target.checked)}
        />
        <span className="lw-checkbox-box"></span>
        <span className="lw-checkbox-label" style={{ lineHeight: 1.15 }}>{t('flower.teleport')}</span>
      </label>
      {([
        { label: t('flower.radius'), value: sim.flowerRadius, set: sim.setFlowerRadius, unit: 'm', min: 1, step: 5 },
        { label: t('flower.segments'), value: sim.flowerSegments, set: sim.setFlowerSegments, unit: t('flower.seg_unit'), min: 3, max: 20, step: 1 },
        { label: t('flower.circles'), value: sim.flowerCircles, set: sim.setFlowerCircles, unit: t('flower.circle_unit'), min: 0.5, step: 0.5 },
        { label: t('flower.rounds'), value: sim.flowerRounds, set: sim.setFlowerRounds, unit: t('flower.round_unit'), min: 1, step: 1 },
        { label: t('flower.pre_wait'), value: sim.flowerPreWait, set: sim.setFlowerPreWait, unit: t('flower.seconds'), min: 0, step: 1 },
        { label: t('flower.post_wait'), value: sim.flowerPostWait, set: sim.setFlowerPostWait, unit: t('flower.seconds'), min: 0, step: 1 },
      ] as const).map((row, ri) => (
        <div key={ri} style={{ display: 'flex', gap: 6, alignItems: 'center', marginBottom: 6 }}>
          <span style={{ opacity: 0.75, flex: 1, whiteSpace: 'nowrap' }}>{row.label}</span>
          <NumberField
            value={row.value}
            set={row.set}
            min={row.min}
            max={(row as { max?: number }).max}
            step={row.step}
          />
          <span style={{ opacity: 0.5, width: 18, textAlign: 'left' }}>{row.unit}</span>
        </div>
      ))}
      <div style={{ opacity: 0.45, fontSize: 10, marginTop: 2 }}>{t('flower.segments_hint')}</div>
      {flowerEstimateSec != null && sim.waypoints.length > 0 && (
        <div style={{
          marginTop: 8, paddingTop: 8,
          borderTop: '1px solid rgba(255,255,255,0.08)',
          display: 'flex', alignItems: 'baseline', gap: 8,
        }}>
          <span style={{ opacity: 0.75 }}>{t('flower.est_total')}</span>
          <span style={{ fontWeight: 700, fontSize: 13, color: '#ffd266' }}>
            {fmtDuration(flowerEstimateSec)}
          </span>
          <span style={{ opacity: 0.4, fontSize: 9, marginLeft: 'auto', textAlign: 'right', lineHeight: 1.3 }}>
            {t('flower.est_hint')}
          </span>
        </div>
      )}
    </div>
  )
}

export default React.memo(FlowerSettingsPanel)
