import { useCallback, useMemo, useState } from 'react'
import { UserAvatar, avatarToHtml, loadAvatar, saveAvatar, loadCustomPng, saveCustomPng } from '../userAvatars'

// Active avatar selection + persistent custom-PNG slot. Stored in two
// separate localStorage keys so picking a preset doesn't drop the user's
// uploaded image. Also owns the picker-dialog open state so App only has
// to wire the picker component itself.
export function useAvatar() {
  const [userAvatar, setUserAvatar] = useState<UserAvatar>(() => loadAvatar())
  const [customPng, setCustomPng] = useState<string | null>(() => loadCustomPng())
  const [pickerOpen, setPickerOpen] = useState(false)

  const save = useCallback((next: UserAvatar, nextCustom: string | null) => {
    setUserAvatar(next)
    saveAvatar(next)
    setCustomPng(nextCustom)
    saveCustomPng(nextCustom)
  }, [])
  const togglePicker = useCallback(() => setPickerOpen((v) => !v), [])
  const closePicker = useCallback(() => setPickerOpen(false), [])

  // Rendered marker HTML for the map. avatarToHtml is pure, so memoizing
  // keeps MapView's prop referentially stable between avatar changes.
  const avatarHtml = useMemo(() => avatarToHtml(userAvatar, customPng), [userAvatar, customPng])

  return useMemo(() => ({
    userAvatar,
    customPng,
    pickerOpen,
    avatarHtml,
    save,
    togglePicker,
    closePicker,
  }), [userAvatar, customPng, pickerOpen, avatarHtml, save, togglePicker, closePicker])
}
