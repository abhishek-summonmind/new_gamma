import { api } from './client';

/**
 * Dhan configuration status with source information
 */
export interface DhanConfigStatus {
  access_token: {
    configured: boolean;
    source: string | null;
  };
  option_expiry: {
    configured: boolean;
    source: string | null;
    value: string | null;
  };
}

/**
 * Request payload for updating Dhan configuration
 */
export interface DhanConfigRequest {
  access_token?: string;
  option_expiry?: string;
}

/**
 * Response from Dhan configuration API
 */
export interface DhanConfigResponse {
  status: string;
  updated?: Record<string, string>;
  message?: string;
  config: DhanConfigStatus;
}

/**
 * API service for Dhan configuration management
 */
export const dhanConfigApi = {
  /**
   * Get current Dhan configuration status
   */
  async getConfig(): Promise<DhanConfigStatus> {
    const response = await api.get<DhanConfigStatus>('/dhan/config');
    return response.data;
  },

  /**
   * Update Dhan configuration (access token and/or option expiry)
   */
  async setConfig(config: DhanConfigRequest): Promise<DhanConfigResponse> {
    const response = await api.post<DhanConfigResponse>('/dhan/config', config);
    return response.data;
  },

  /**
   * Clear Dhan configuration from Redis (revert to .env values)
   */
  async clearConfig(): Promise<DhanConfigResponse> {
    const response = await api.delete<DhanConfigResponse>('/dhan/config');
    return response.data;
  },

};
