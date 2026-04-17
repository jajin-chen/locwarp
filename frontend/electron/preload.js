const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  locatePc: () => ipcRenderer.invoke('locate-pc'),
})
