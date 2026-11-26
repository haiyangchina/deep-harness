import { contextBridge, ipcRenderer } from 'electron'
import type { DesktopBridge } from '../src/protocol'

const bridge: DesktopBridge = {
  info: () => ipcRenderer.invoke('deep-harness:info'),
  start: settings => ipcRenderer.invoke('deep-harness:start', settings),
  stop: () => ipcRenderer.invoke('deep-harness:stop'),
  openProject: id => ipcRenderer.invoke('deep-harness:project', id),
  choose: kind => ipcRenderer.invoke('deep-harness:choose', kind),
  request: request => ipcRenderer.invoke('deep-harness:request', request)
}
contextBridge.exposeInMainWorld('deepHarness', bridge)
