import { renderHook, act } from '@testing-library/react'
import { useUpload } from './useUpload'

describe('useUpload', () => {
  it('calls onSuccess with filename and volume_path on 200', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ volume_path: '/Volumes/cat/sch/uploads/abc.pdf', size_bytes: 1024 }),
    } as unknown as Response))

    const onSuccess = vi.fn()
    const { result } = renderHook(() => useUpload(onSuccess))

    const file = new File(['%PDF-1.4'], 'my-contract.pdf', { type: 'application/pdf' })

    await act(async () => { await result.current.upload(file) })

    expect(result.current.status).toBe('idle')
    expect(onSuccess).toHaveBeenCalledWith('my-contract.pdf', '/Volumes/cat/sch/uploads/abc.pdf')

    const [url, init] = vi.mocked(globalThis.fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/upload-contract')
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
  })

  it('sets status to error on HTTP error response', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false,
      status: 413,
    } as unknown as Response))

    const onSuccess = vi.fn()
    const { result } = renderHook(() => useUpload(onSuccess))
    const file = new File(['%PDF-1.4'], 'big.pdf', { type: 'application/pdf' })

    await act(async () => { await result.current.upload(file) })

    expect(result.current.status).toBe('error')
    expect(onSuccess).not.toHaveBeenCalled()

    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(result.current.status).toBe('idle')
    vi.useRealTimers()
  })

  it('sets status to error on network failure', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('network down')))

    const onSuccess = vi.fn()
    const { result } = renderHook(() => useUpload(onSuccess))
    const file = new File(['%PDF-1.4'], 'contract.pdf', { type: 'application/pdf' })

    await act(async () => { await result.current.upload(file) })

    expect(result.current.status).toBe('error')
    await act(async () => { vi.advanceTimersByTime(4000) })
    expect(result.current.status).toBe('idle')
    vi.useRealTimers()
  })
})
