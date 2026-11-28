import { useEffect, useRef, useState } from 'react'
import type { ClipboardEvent, DragEvent } from 'react'

export type ImageDraft = { file: File } | { id: string }
export type ImageSource = (id: string, full?: boolean) => string | Promise<string>

const maxImages = 4
const maxBytes = 8 * 1024 * 1024
const formats = ['image/png', 'image/jpeg']

export function isSupportedImage(file: File) {
  return formats.includes(file.type) || /\.(png|jpe?g)$/i.test(file.name)
}

export function appendImages(current: ImageDraft[], items: ImageDraft[]) {
  const added: ImageDraft[] = []
  let error = ''
  for (const item of items) {
    if ('file' in item && !isSupportedImage(item.file)) {
      error = '请选择 PNG 或 JPEG 图片。'
    } else if ('file' in item && item.file.size > maxBytes) {
      error = '单张图片最大 8 MiB。'
    } else if (current.length + added.length >= maxImages) {
      error = '每条消息最多发送 4 张图片。'
    } else if (!('id' in item) || ![...current, ...added].some(row => 'id' in row && row.id === item.id)) {
      added.push(item)
    }
  }
  return { images: [...current, ...added], error }
}

export function pasteImages(event: ClipboardEvent, add: (files: File[]) => void) {
  const images = Array.from(event.clipboardData.files).filter(file => file.type.startsWith('image/'))
  if (!images.length) return
  if (!event.clipboardData.getData('text/plain')) event.preventDefault()
  add(images)
}

export function dropFiles(event: DragEvent, add: (files: File[]) => void, disabled: boolean) {
  if (!event.dataTransfer.types.includes('Files')) return
  event.preventDefault()
  if (!disabled) add(Array.from(event.dataTransfer.files))
}

export function FilePicker({ add, disabled }: {
  add: (files: File[]) => void
  disabled: boolean
}) {
  return <label className="image-picker image-picker-icon" title="添加图片或附件">
    <span className="image-picker-plus" aria-hidden="true" />
    <input type="file" aria-label="选择图片或附件" multiple
      disabled={disabled} onChange={event => {
        add(Array.from(event.currentTarget.files ?? []))
        event.currentTarget.value = ''
      }} />
  </label>
}

function useImageSource(image: ImageDraft, load: ImageSource, full = false, enabled = true) {
  const file = 'file' in image ? image.file : undefined
  const id = 'id' in image ? image.id : undefined
  const [src, setSrc] = useState('')
  const [unavailable, setUnavailable] = useState(false)
  useEffect(() => {
    setUnavailable(false)
    setSrc('')
    if (!enabled) return
    if (file) {
      const url = URL.createObjectURL(file)
      setSrc(url)
      return () => URL.revokeObjectURL(url)
    }
    let active = true
    let ownedUrl = ''
    if (id) Promise.resolve().then(() => load(id, full)).then(url => {
      if (url.startsWith('blob:')) ownedUrl = url
      if (active) setSrc(url)
      else if (ownedUrl) URL.revokeObjectURL(ownedUrl)
    }).catch(() => {
      if (active) setUnavailable(true)
    })
    return () => { active = false; if (ownedUrl) URL.revokeObjectURL(ownedUrl) }
  }, [file, id, load, enabled, full])
  return { src, unavailable }
}

function Picture({ image, load, onRemove, onReuse, expired, disabled }: {
  image: ImageDraft; load: ImageSource; onRemove?: () => void; onReuse?: () => void
  expired?: boolean; disabled?: boolean
}) {
  const { src, unavailable } = useImageSource(image, load)
  const dialog = useRef<HTMLDialogElement>(null)
  const [opened, setOpened] = useState(false)
  const full = useImageSource(image, load, true, opened && 'id' in image)
  const label = 'file' in image ? image.file.name : '会话图片'
  return <div className="message-picture">
    {src ? <button type="button" className="image-preview" aria-label={`查看${label}`}
      onClick={() => { setOpened(true); dialog.current?.showModal() }}><img src={src} alt={label} loading="lazy" /></button>
      : <span>{unavailable ? '图片不可用' : '读取图片…'}</span>}
    {src && <dialog ref={dialog} className="image-dialog" aria-label={label} onClose={() => setOpened(false)}>
      <button type="button" onClick={() => dialog.current?.close()}>关闭</button>
      <img src={full.src || src} alt={label} onError={() => dialog.current?.close()} />
    </dialog>}
    {onRemove && <button type="button" disabled={disabled} onClick={onRemove}>移除</button>}
    {onReuse && <button type="button" disabled={disabled} onClick={onReuse}>引用图片</button>}
    {expired && <small>已移出模型上下文，可重新引用</small>}
  </div>
}

export function ImageStrip({ images, load, remove, reuse, context, disabled }: {
  images: ImageDraft[]; load: ImageSource; remove?: (index: number) => void
  reuse?: (id: string) => void; context?: string[]; disabled?: boolean
}) {
  if (!images.length) return null
  return <div className="image-strip">{images.map((image, index) =>
    <Picture key={'id' in image ? image.id : `${image.file.name}:${index}`} image={image} load={load}
      onRemove={remove && (() => remove(index))}
      disabled={disabled}
      expired={context && 'id' in image && !context.includes(image.id)}
      onReuse={'id' in image && reuse ? () => reuse(image.id) : undefined} />
  )}</div>
}


export async function stageImages(
  images: ImageDraft[], upload: (file: File) => Promise<string>, progress: (images: ImageDraft[]) => void
) {
  const staged = [...images]
  const ids: string[] = []
  for (const [index, image] of images.entries()) {
    const id = 'id' in image ? image.id : await upload(image.file)
    ids.push(id)
    if ('file' in image) {
      staged[index] = { id }
      progress([...staged])
    }
  }
  return ids
}
