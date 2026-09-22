import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import * as DhanConfig from '../services/dhanConfigApi'
import { refresh } from '../services/refreshApi'
import { getSelectedIndexSymbol } from '../services/indexSelection'
import { useConfigStore } from "../stores/configStore";

type DhanConfigStatus = DhanConfig.DhanConfigStatus

interface DhanConfigModalProps {
  isOpen: boolean
  onClose: () => void
}

export default function DhanConfigModal({
  isOpen,
  onClose,
}: DhanConfigModalProps) {
  const queryClient = useQueryClient()
  const [config, setConfig] = useState<DhanConfigStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  // Form state
  const [accessToken, setAccessToken] = useState('')
  const [optionExpiry, setOptionExpiry] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const { setOptionExpiry: setConfigOptionExpiry } = useConfigStore();

  // Load current config when modal opens
  useEffect(() => {
    if (!isOpen) return
    let active = true

    DhanConfig.dhanConfigApi
      .getConfig()
      .then((data) => {
        if (!active) return
        setConfig(data)
        setAccessToken('')
        setOptionExpiry(data.option_expiry.value || '')
        setError(null)
      })
      .catch((err) => {
        if (!active) return
        setError(err instanceof Error ? err.message : 'Failed to load config')
      })

    return () => {
      active = false
    }
  }, [isOpen])

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setError(null)
    setSuccess(null)

    if (!accessToken && !optionExpiry) {
      setError('Please enter at least one field')
      return
    }

    try {
      setLoading(true)

      const response = await DhanConfig.dhanConfigApi.setConfig({
        access_token: accessToken || undefined,
        option_expiry: optionExpiry || undefined,
      })

      const selectedIndex = getSelectedIndexSymbol()
      console.log('Response after setConfig:', response)
      try {
        await refresh(selectedIndex)
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: ['s9'] }),
          queryClient.invalidateQueries({ queryKey: ['s1'] }),
        ])

        if (response.updated?.option_expiry) {
  setConfigOptionExpiry(response.updated.option_expiry);
}
      } catch (refreshError) {
        setConfig(response.config)
        setAccessToken('')
        const message =
          refreshError instanceof Error ? refreshError.message : 'unknown refresh error'
        throw new Error(
          `Configuration was saved, but ${selectedIndex} refresh failed: ${message}`,
          { cause: refreshError },
        )
      }

      setSuccess(
        `Configuration applied to ${selectedIndex}: ${Object.keys(
          response.updated || {},
        ).join(', ')}`,
      )
      setConfig(response.config)
      setAccessToken('')

      window.setTimeout(() => {
        setSuccess(null)
      }, 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update config')
    } finally {
      setLoading(false)
    }
  }

  const handleClear = async () => {
    const shouldClear = window.confirm(
      'Clear all broker configuration from the database? This will revert to .env values.',
    )

    if (!shouldClear) {
      return
    }

    try {
      setLoading(true)
      setError(null)
      setSuccess(null)

      const response = await DhanConfig.dhanConfigApi.clearConfig()

      setSuccess(response.message || 'Configuration cleared')
      setConfig(response.config)
      setAccessToken('')
      setOptionExpiry('')

      window.setTimeout(() => {
        setSuccess(null)
      }, 3000)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear config')
    } finally {
      setLoading(false)
    }
  }

  if (!isOpen) {
    return null
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="dhan-config-title"
        className="max-h-[90vh] w-full max-w-md overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-2xl"
      >
        {/* Header */}
        <div className="sticky top-0 z-10 flex items-center justify-between bg-gradient-to-r from-blue-600 to-blue-700 px-6 py-5 text-white">
          <div>
            <h2
              id="dhan-config-title"
              className="text-xl font-bold tracking-tight"
            >
              Broker Configuration
            </h2>
            <p className="mt-1 text-xs text-blue-100">
              Manage runtime Groww access token and option expiry
            </p>
          </div>

          <button
            type="button"
            onClick={onClose}
            aria-label="Close broker configuration"
            className="flex h-9 w-9 items-center justify-center rounded-full text-xl text-white transition hover:bg-white/15 focus:outline-none focus:ring-2 focus:ring-white/70"
          >
            ×
          </button>
        </div>

        {/* Content */}
        <div className="max-h-[calc(90vh-88px)] overflow-y-auto bg-white p-6">
          {/* Status Section */}
          {config && (
            <div className="mb-6 rounded-xl border border-gray-200 bg-gray-50 p-4">
              <h3 className="mb-4 text-sm font-semibold text-gray-900">
                Current Status
              </h3>

              <div className="space-y-4">
                {/* Access Token Status */}
                <div className="flex items-center justify-between gap-4">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-gray-800">
                      Access Token
                    </p>
                    <p className="mt-0.5 truncate text-xs text-gray-500">
                      {config.access_token.source || 'Not set'}
                    </p>
                  </div>

                  <span
                    className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold ${
                      config.access_token.configured
                        ? 'bg-green-100 text-green-800'
                        : 'bg-red-100 text-red-800'
                    }`}
                  >
                    {config.access_token.configured ? 'Set' : 'Missing'}
                  </span>
                </div>

                </div>

                {/* Option Expiry Status */}
                <div className="flex items-center justify-between gap-4 border-t border-gray-200 pt-4">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-gray-800">
                      Option Expiry
                    </p>
                    <p className="mt-0.5 truncate text-xs text-gray-500">
                      {config.option_expiry.value
                        ? `${config.option_expiry.value} (${
                            config.option_expiry.source || 'Unknown source'
                          })`
                        : 'Not set'}
                    </p>
                  </div>

                  <span
                    className={`shrink-0 rounded-full px-3 py-1 text-xs font-semibold ${
                      config.option_expiry.configured
                        ? 'bg-green-100 text-green-800'
                        : 'bg-red-100 text-red-800'
                    }`}
                  >
                    {config.option_expiry.configured ? 'Set' : 'Missing'}
                  </span>
                </div>
              </div>
          )}

          {/* Error Message */}
          {error && (
            <div
              role="alert"
              className="mb-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm font-medium text-red-700"
            >
              {error}
            </div>
          )}

          {/* Success Message */}
          {success && (
            <div
              role="status"
              className="mb-4 rounded-xl border border-green-200 bg-green-50 px-4 py-3 text-sm font-medium text-green-700"
            >
              {success}
            </div>
          )}

          {/* Form */}
          {!loading && (
            <form onSubmit={handleSubmit} className="space-y-5">
              {/* Access Token Field */}
              <div>
                <label
                  htmlFor="dhan-access-token"
                  className="mb-2 block text-sm font-semibold text-gray-800"
                >
                  GROWW_ACCESS_TOKEN
                  <span className="ml-1 font-normal text-gray-500">
                    (Optional)
                  </span>
                </label>

                <div className="relative">
                  <input
                    id="dhan-access-token"
                    type={showPassword ? 'text' : 'password'}
                    value={accessToken}
                    onChange={(e) => setAccessToken(e.target.value)}
                    placeholder="Paste new access token here"
                    autoComplete="new-password"
                    className="w-full rounded-xl border border-gray-300 bg-white px-4 py-3 pr-12 font-mono text-sm text-gray-900 caret-blue-600 shadow-sm outline-none transition placeholder:text-gray-400 focus:border-blue-500 focus:ring-4 focus:ring-blue-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-500"
                  />

                  <button
                    type="button"
                    onClick={() => setShowPassword((current) => !current)}
                    aria-label={
                      showPassword ? 'Hide access token' : 'Show access token'
                    }
                    className="absolute right-2 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-lg text-gray-600 transition hover:bg-gray-100 hover:text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    {showPassword ? '🙈' : '👁️'}
                  </button>
                </div>

                <p className="mt-1.5 text-xs text-gray-500">
                  Paste a fresh Groww access token when Groww returns authentication failed.
                </p>
              </div>

              {/* Option Expiry Field */}
              <div>
                <label
                  htmlFor="dhan-option-expiry"
                  className="mb-2 block text-sm font-semibold text-gray-800"
                >
                  OPTION_EXPIRY
                  <span className="ml-1 font-normal text-gray-500">
                    (Optional)
                  </span>
                </label>

                <input
                  id="dhan-option-expiry"
                  type="date"
                  value={optionExpiry}
                  onChange={(e) => setOptionExpiry(e.target.value)}
                  className="w-full rounded-xl border border-gray-300 bg-white px-4 py-3 text-sm text-gray-900 caret-blue-600 shadow-sm outline-none transition [color-scheme:light] focus:border-blue-500 focus:ring-4 focus:ring-blue-100 disabled:cursor-not-allowed disabled:bg-gray-100 disabled:text-gray-500"
                />

                <p className="mt-1.5 text-xs text-gray-500">
                  Select the option expiry date in YYYY-MM-DD format.
                </p>
              </div>

              {/* Action Buttons */}
              <div className="grid grid-cols-2 gap-3 pt-2">
                <button
                  type="submit"
                  disabled={loading}
                  className="rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 focus:outline-none focus:ring-4 focus:ring-blue-200 disabled:cursor-not-allowed disabled:bg-gray-400"
                >
                  {loading ? 'Updating...' : 'Update'}
                </button>

                <button
                  type="button"
                  onClick={handleClear}
                  disabled={loading}
                  className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-2.5 text-sm font-semibold text-amber-800 transition hover:bg-amber-100 focus:outline-none focus:ring-4 focus:ring-amber-100 disabled:cursor-not-allowed disabled:border-gray-300 disabled:bg-gray-100 disabled:text-gray-400"
                >
                  {loading ? 'Clearing...' : 'Clear'}
                </button>
              </div>
            </form>
          )}

          {/* Loading State */}
          {loading && (
            <div className="flex flex-col items-center justify-center py-10">
              <div className="h-9 w-9 animate-spin rounded-full border-4 border-blue-100 border-t-blue-600" />
              <p className="mt-3 text-sm font-medium text-gray-600">
                Processing configuration...
              </p>
            </div>
          )}

          {/* Info Box */}
          <div className="mt-6 rounded-xl border border-blue-200 bg-blue-50 p-4">
            <p className="mb-2 text-sm font-semibold text-blue-900">
              How it works
            </p>

            <ul className="space-y-1.5 text-xs leading-5 text-blue-700">
              <li>Updates are stored in the database without requiring a restart.</li>
              <li>The database value takes priority over .env values.</li>
              <li>Clear removes database values and restores .env fallback.</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
