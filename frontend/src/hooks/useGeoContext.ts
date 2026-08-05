import { useEffect, useRef, useState } from 'react'
import * as api from '../services/api'

// Reverse-geo-derived state used by the status bar: country-code flag and
// (later) timezone tag. Populated debounced from the sim's current position
// so we don't hit Nominatim/Photon every position_update tick.
export interface LocMeta {
  countryCode: string
  // Reverse-geocoded city / POI / road name (whatever Photon-or-Nominatim's
  // short_name returns). Used by the timezone-detail modal in StatusBar
  // to print "Country City" alongside the IANA zone, and may be empty
  // if the lookup failed or the spot is mid-ocean.
  cityName: string
  timezoneZone: string | null
  gmtOffsetSeconds: number | null
  // Weather at the current virtual location. Fetched from Open-Meteo when
  // the position moves >=100m and the sim is quiescent (same gate as
  // reverse-geocode + timezone). Null = unknown / not yet fetched.
  weatherCode: number | null
  tempC: number | null
}

// Reverse-geocode + timezone lookup, tied to the current virtual location
// but GATED so it only fires on discrete user-initiated moves (teleport,
// bookmark tap, manual coord entry). During active navigate / loop /
// multi-stop / random-walk the simulation engine emits a position update
// every tick, which used to spam Nominatim + TimezoneDB every second and
// contend with the USB DVT channel — contributed to users seeing random
// walk 'freeze' (see backend log 2026-04-16 user report).
//
// Rule: only look up when the sim state is idle / teleporting / disconnected
// (i.e. no route animation in flight), AND the position actually moved
// >=100m from the last looked-up point.
//
// The returned LocMeta is a single useState object, so its identity is
// stable across renders until a lookup actually lands.
export function useGeoContext(
  currentPosition: { lat: number; lng: number } | null,
  simState: string | undefined,
): LocMeta {
  const [locMeta, setLocMeta] = useState<LocMeta>({
    countryCode: '', cityName: '', timezoneZone: null, gmtOffsetSeconds: null,
    weatherCode: null, tempC: null,
  })
  // Last position we successfully looked up reverse-geo/timezone for. Used
  // to suppress redundant lookups when jitter nudges the coordinate but the
  // user hasn't actually moved.
  const lastLookedUpPosRef = useRef<{ lat: number; lng: number } | null>(null)

  useEffect(() => {
    const pos = currentPosition
    if (!pos) return
    const state = simState ?? 'idle'
    const isQuiescent = state === 'idle' || state === 'teleporting' || state === 'disconnected'
    if (!isQuiescent) return
    // Skip redundant lookups when the user stays at the same spot (jitter
    // within 100m of the last resolved position).
    const last = lastLookedUpPosRef.current
    if (last) {
      const dLat = (pos.lat - last.lat) * 111320
      const dLng = (pos.lng - last.lng) * 111320 * Math.cos(pos.lat * Math.PI / 180)
      if (dLat * dLat + dLng * dLng < 100 * 100) return
    }
    let cancelled = false
    const tid = setTimeout(() => {
      lastLookedUpPosRef.current = { lat: pos.lat, lng: pos.lng }
      // Three independent services (Nominatim, TimezoneDB, Open-Meteo).
      // Fire in parallel so one outage doesn't freeze the other two for
      // a 10s timeout each time the position changes.
      void api.reverseGeocode(pos.lat, pos.lng).then((geoRes: any) => {
        if (cancelled) return
        const cc = String(geoRes?.country_code ?? '').toLowerCase()
        const city = String(geoRes?.short_name ?? '').trim()
        setLocMeta((prev) =>
          (prev.countryCode === cc && prev.cityName === city)
            ? prev
            : { ...prev, countryCode: cc, cityName: city }
        )
      }).catch(() => { /* offline / rate-limited — keep previous */ })
      void api.lookupTimezone(pos.lat, pos.lng).then((tz) => {
        if (cancelled || !tz) return
        setLocMeta((prev) => ({ ...prev, timezoneZone: tz.zone, gmtOffsetSeconds: tz.gmt_offset_seconds }))
      }).catch(() => { /* ignore */ })
      void api.lookupWeather(pos.lat, pos.lng).then((wx) => {
        if (cancelled || !wx) return
        setLocMeta((prev) => prev.weatherCode === wx.code && prev.tempC === wx.tempC
          ? prev
          : { ...prev, weatherCode: wx.code, tempC: wx.tempC })
      }).catch(() => { /* ignore */ })
    }, 600)
    return () => { cancelled = true; clearTimeout(tid) }
  }, [currentPosition?.lat, currentPosition?.lng, simState])

  return locMeta
}
