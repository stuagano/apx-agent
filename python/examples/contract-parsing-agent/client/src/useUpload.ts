import { useState, useCallback } from 'react'

export type UploadStatus = 'idle' | 'uploading' | 'error'

export function useUpload(onSuccess: (filename: string, volumePath: string) => void) {
  const [status, setStatus] = useState<UploadStatus>('idle')

  const upload = useCallback(async (file: File) => {
    setStatus('uploading')
    try {
      const fd = new FormData()
      fd.append('file', file)
      const resp = await fetch('/api/upload-contract', { method: 'POST', body: fd })
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const { volume_path } = await resp.json() as { volume_path: string; size_bytes: number }
      setStatus('idle')
      onSuccess(file.name, volume_path)
    } catch {
      setStatus('error')
      setTimeout(() => setStatus('idle'), 4000)
    }
  }, [onSuccess])

  return { status, upload }
}
