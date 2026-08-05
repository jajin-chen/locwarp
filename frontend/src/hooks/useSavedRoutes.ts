import { useCallback, useEffect, useMemo, useState } from 'react'
import * as api from '../services/api'
import { useSimulation } from './useSimulation'
import { useT } from '../i18n'

type Sim = ReturnType<typeof useSimulation>
type Translate = ReturnType<typeof useT>

// Saved-route library: route + category CRUD, reorder, and GPX / JSON
// import-export. Owns the savedRoutes / routeCategories lists (loaded once
// on mount) and every handler ControlPanel's route library needs. The
// route-LOAD confirm flow stays in App because it orchestrates across
// domains (teleport fan-out, map fitBounds, recent-places push).
export function useSavedRoutes({ sim, showToast, t }: {
  sim: Sim
  showToast: (msg: string, ms?: number) => void
  t: Translate
}) {
  const [savedRoutes, setSavedRoutes] = useState<any[]>([])
  const [routeCategories, setRouteCategories] = useState<any[]>([])
  const refreshRouteCategories = useCallback(async () => {
    try {
      const cats = await api.listRouteCategories()
      setRouteCategories(Array.isArray(cats) ? cats : [])
    } catch { /* leave empty so RouteList still falls back to default */ }
  }, [])

  // Load saved routes + categories on mount
  useEffect(() => {
    api.getSavedRoutes().then(setSavedRoutes).catch((err: any) => {
      // Surface the failure instead of silently leaving the route list
      // empty — the user would otherwise have no hint a reload could fix it.
      showToast(err?.message || 'failed to load saved routes')
    })
    refreshRouteCategories()
  }, [refreshRouteCategories, showToast])

  const handleRouteSave = useCallback(async (
    name: string,
    opts?: { categoryId?: string; overwriteId?: string },
  ) => {
    if (sim.waypoints.length === 0) {
      showToast(t('toast.route_need_waypoint'))
      return
    }
    try {
      if (opts?.overwriteId) {
        // Find the existing route so we keep its category_id when the user
        // doesn't explicitly choose a different one (the "save and overwrite"
        // flow). Falls back to default if missing.
        const prev = savedRoutes.find((r) => r.id === opts.overwriteId)
        await api.replaceRoute(opts.overwriteId, {
          id: opts.overwriteId,
          name,
          waypoints: sim.waypoints,
          profile: sim.moveMode,
          category_id: opts.categoryId ?? prev?.category_id ?? 'default',
        })
        const routes = await api.getSavedRoutes()
        setSavedRoutes(routes)
        showToast(t('toast.route_overwritten', { name }))
        return
      }
      await api.saveRoute({
        name,
        waypoints: sim.waypoints,
        profile: sim.moveMode,
        category_id: opts?.categoryId ?? 'default',
      })
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
      showToast(t('toast.route_saved', { name }))
    } catch (err: any) {
      showToast(t('toast.route_save_failed', { msg: err.message || '' }))
    }
  }, [sim, savedRoutes, showToast, t])

  const handleRoutesBulkDelete = useCallback(async (ids: string[]) => {
    try {
      await Promise.all(ids.map((id) => api.deleteRoute(id).catch(() => null)))
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
      showToast(t('toast.route_bulk_deleted', { n: ids.length }))
    } catch (err: any) {
      showToast(err.message || t('toast.route_delete_failed'))
    }
  }, [showToast, t])

  const handleRouteMove = useCallback(async (ids: string[], targetCategoryId: string) => {
    try {
      await api.moveRoutes(ids, targetCategoryId)
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
    } catch (err: any) {
      showToast(err.message || 'move failed')
    }
  }, [showToast])

  const handleRouteCategoryAdd = useCallback(async (name: string, color = '#6c8cff') => {
    try {
      await api.createRouteCategory(name, color)
      await refreshRouteCategories()
    } catch (err: any) {
      showToast(err.message || 'category add failed')
    }
  }, [refreshRouteCategories, showToast])

  const handleRouteCategoryDelete = useCallback(async (id: string) => {
    try {
      await api.deleteRouteCategory(id)
      // Routes that pointed at this category were moved to default by the
      // backend; refresh both lists so the UI reflects the regrouped state.
      await refreshRouteCategories()
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
    } catch (err: any) {
      showToast(err.message || 'category delete failed')
    }
  }, [refreshRouteCategories, showToast])

  const handleRouteCategoryRename = useCallback(async (id: string, name: string) => {
    try {
      const cat = routeCategories.find((c) => c.id === id)
      await api.updateRouteCategory(id, { name, color: cat?.color || '#6c8cff' })
      await refreshRouteCategories()
    } catch (err: any) {
      showToast(err.message || 'category rename failed')
    }
  }, [routeCategories, refreshRouteCategories, showToast])

  const handleRouteCategoryRecolor = useCallback(async (id: string, color: string) => {
    try {
      const cat = routeCategories.find((c) => c.id === id)
      await api.updateRouteCategory(id, { name: cat?.name || '', color })
      await refreshRouteCategories()
    } catch (err: any) {
      showToast(err.message || 'category recolor failed')
    }
  }, [routeCategories, refreshRouteCategories, showToast])

  const handleRouteCategoryReorder = useCallback(async (orderedIds: string[]) => {
    // Optimistically reorder locally so the arrow click feels instant; refresh
    // reconciles with the backend afterwards.
    setRouteCategories((prev) => {
      const byId = new Map(prev.map((c) => [c.id, c] as const))
      const head = orderedIds.map((id) => byId.get(id)).filter((c): c is any => !!c)
      const headIds = new Set(head.map((c: any) => c.id))
      const tail = prev.filter((c: any) => !headIds.has(c.id))
      return [...head, ...tail]
    })
    try {
      await api.reorderRouteCategories(orderedIds)
      await refreshRouteCategories()
    } catch (err: any) {
      showToast(err.message || 'category reorder failed')
      await refreshRouteCategories()
    }
  }, [refreshRouteCategories, showToast])

  const handleRouteReorder = useCallback(async (categoryId: string, orderedIds: string[]) => {
    // Splice the affected category's routes into the new order while keeping
    // other categories' relative positions intact, mirroring the backend.
    setSavedRoutes((prev) => {
      const order = new Map(orderedIds.map((id, idx) => [id, idx] as const))
      const inCat = prev.filter((r) => (r.category_id || 'default') === categoryId)
      inCat.sort((a, b) => {
        const ai = order.has(a.id) ? (order.get(a.id) as number) : Number.MAX_SAFE_INTEGER
        const bi = order.has(b.id) ? (order.get(b.id) as number) : Number.MAX_SAFE_INTEGER
        return ai - bi
      })
      const result = [...prev]
      let ptr = 0
      for (let i = 0; i < result.length; i++) {
        if ((result[i].category_id || 'default') === categoryId) {
          result[i] = inCat[ptr++]
        }
      }
      return result
    })
    try {
      await api.reorderRoutes(categoryId, orderedIds)
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
    } catch (err: any) {
      showToast(err.message || 'route reorder failed')
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
    }
  }, [showToast])

  const handleGpxImport = useCallback(async (file: File) => {
    try {
      const res = await api.importGpx(file)
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
      showToast(t('toast.gpx_imported', { n: res.points }))
    } catch (err: any) {
      showToast(t('toast.gpx_import_failed', { msg: err.message || '' }))
    }
  }, [showToast])

  const handleGpxExport = useCallback((id: string) => {
    const url = api.exportGpxUrl(id)
    window.open(url, '_blank')
  }, [])

  const handleRoutesImportAll = useCallback(async (file: File) => {
    try {
      const text = await file.text()
      const data = JSON.parse(text)
      if (!Array.isArray(data?.routes)) {
        throw new Error('invalid file: missing routes array')
      }
      // Pass categories through too if present (post-v0.2.133 export shape).
      // Old exports without this field still import fine.
      const res = await api.importAllRoutes({
        routes: data.routes,
        categories: Array.isArray(data?.categories) ? data.categories : [],
      })
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
      await refreshRouteCategories()
      showToast(t('toast.routes_imported', { n: res.imported }))
    } catch (err: any) {
      showToast(t('toast.routes_import_failed', { msg: err.message || '' }))
    }
  }, [showToast, refreshRouteCategories, t])

  const handleRouteRename = useCallback(async (id: string, name: string) => {
    try {
      await api.renameRoute(id, name)
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
    } catch (err: any) {
      showToast(err.message || t('toast.route_rename_failed'))
    }
  }, [showToast])

  const handleRouteDelete = useCallback(async (id: string) => {
    try {
      await api.deleteRoute(id)
      const routes = await api.getSavedRoutes()
      setSavedRoutes(routes)
      showToast(t('toast.route_deleted'))
    } catch (err: any) {
      showToast(err.message || t('toast.route_delete_failed'))
    }
  }, [showToast])

  return useMemo(() => ({
    savedRoutes,
    routeCategories,
    handleRouteSave,
    handleRoutesBulkDelete,
    handleRouteMove,
    handleRouteCategoryAdd,
    handleRouteCategoryDelete,
    handleRouteCategoryRename,
    handleRouteCategoryRecolor,
    handleRouteCategoryReorder,
    handleRouteReorder,
    handleGpxImport,
    handleGpxExport,
    handleRoutesImportAll,
    handleRouteRename,
    handleRouteDelete,
  }), [
    savedRoutes, routeCategories,
    handleRouteSave, handleRoutesBulkDelete, handleRouteMove,
    handleRouteCategoryAdd, handleRouteCategoryDelete, handleRouteCategoryRename,
    handleRouteCategoryRecolor, handleRouteCategoryReorder, handleRouteReorder,
    handleGpxImport, handleGpxExport, handleRoutesImportAll,
    handleRouteRename, handleRouteDelete,
  ])
}
