import { useState, useCallback, useEffect, useRef } from 'react'
import * as api from '../services/api'

export interface Bookmark {
  id: string
  name: string
  lat: number
  lng: number
  category_id?: string
  note?: string
  created_at?: string
}

export interface BookmarkCategory {
  id: string
  name: string
  color?: string
  sort_order?: number
}

export function useBookmarks() {
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([])
  const [categories, setCategories] = useState<BookmarkCategory[]>([])
  const [loading, setLoading] = useState(false)
  const mountedRef = useRef(true)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [bms, cats] = await Promise.all([
        api.getBookmarks(),
        api.getCategories(),
      ])
      if (!mountedRef.current) return
      setBookmarks(Array.isArray(bms) ? bms : bms.bookmarks ?? [])
      setCategories(Array.isArray(cats) ? cats : [])
    } catch (err) {
      console.error('Failed to load bookmarks:', err)
    } finally {
      if (mountedRef.current) setLoading(false)
    }
  }, [])

  // Load on mount
  useEffect(() => {
    mountedRef.current = true
    refresh()
    return () => {
      mountedRef.current = false
    }
  }, [refresh])

  const createBookmark = useCallback(
    async (bm: Omit<Bookmark, 'id'>) => {
      const created = await api.createBookmark(bm)
      await refresh()
      return created
    },
    [refresh],
  )

  const deleteBookmark = useCallback(
    async (id: string) => {
      await api.deleteBookmark(id)
      setBookmarks((prev) => prev.filter((b) => b.id !== id))
    },
    [],
  )

  const updateBookmark = useCallback(
    async (id: string, data: Partial<Bookmark>) => {
      const updated = await api.updateBookmark(id, data)
      await refresh()
      return updated
    },
    [refresh],
  )

  const moveBookmarks = useCallback(
    async (ids: string[], categoryId: string) => {
      await api.moveBookmarks(ids, categoryId)
      await refresh()
    },
    [refresh],
  )

  const createCategory = useCallback(
    async (cat: Omit<BookmarkCategory, 'id'>) => {
      const created = await api.createCategory(cat)
      await refresh()
      return created
    },
    [refresh],
  )

  const deleteCategory = useCallback(
    async (id: string) => {
      await api.deleteCategory(id)
      await refresh()
    },
    [refresh],
  )

  const updateCategory = useCallback(
    async (id: string, data: Partial<BookmarkCategory>) => {
      const updated = await api.updateCategory(id, data)
      await refresh()
      return updated
    },
    [refresh],
  )

  const reorderCategories = useCallback(
    async (categoryIds: string[]) => {
      // Optimistic local reorder so the dragged row stays in place visually
      // while the API round-trip completes. refresh() reconciles to truth.
      setCategories((prev) => {
        const byId = new Map(prev.map((c) => [c.id, c] as const))
        const head = categoryIds
          .map((id) => byId.get(id))
          .filter((c): c is BookmarkCategory => !!c)
        const headIds = new Set(head.map((c) => c.id))
        const tail = prev.filter((c) => !headIds.has(c.id))
        return [...head, ...tail]
      })
      await api.reorderBookmarkCategories(categoryIds)
      await refresh()
    },
    [refresh],
  )

  const reorderBookmarksInCategory = useCallback(
    async (categoryId: string, bookmarkIds: string[]) => {
      setBookmarks((prev) => {
        const order = new Map(bookmarkIds.map((id, idx) => [id, idx] as const))
        // Stable sort: items in the affected category sort by the new order
        // (items outside the order list go to the end of their category).
        // Items in other categories keep their relative position.
        const inCat: typeof prev = []
        const outCat: typeof prev = []
        for (const b of prev) {
          if (b.category_id === categoryId) inCat.push(b)
          else outCat.push(b)
        }
        inCat.sort((a, b) => {
          const ai = order.has(a.id) ? (order.get(a.id) as number) : Number.MAX_SAFE_INTEGER
          const bi = order.has(b.id) ? (order.get(b.id) as number) : Number.MAX_SAFE_INTEGER
          return ai - bi
        })
        // Splice the in-cat items back into the positions they originally
        // occupied, preserving other-category positions for stable rendering.
        const result = [...prev]
        let inCatPtr = 0
        for (let i = 0; i < result.length; i++) {
          if (result[i].category_id === categoryId) {
            result[i] = inCat[inCatPtr++]
          }
        }
        return result
      })
      await api.reorderBookmarks(categoryId, bookmarkIds)
      await refresh()
    },
    [refresh],
  )

  return {
    bookmarks,
    categories,
    loading,
    createBookmark,
    updateBookmark,
    deleteBookmark,
    moveBookmarks,
    createCategory,
    deleteCategory,
    updateCategory,
    reorderCategories,
    reorderBookmarksInCategory,
    refresh,
  }
}
