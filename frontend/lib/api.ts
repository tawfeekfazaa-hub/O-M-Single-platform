// All dashboard data comes from OUR backend API — never from vendor APIs
// directly (CLAUDE.md architecture rule).

const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type PlantStatus = "healthy" | "faulty" | "disconnected" | "unknown";

export interface KpiPoint {
  ts: string;
  active_power_kw: number | null;
  daily_energy_kwh: number | null;
  total_energy_kwh: number | null;
  performance_ratio: number | null;
}

export interface Plant {
  id: number;
  vendor: string;
  vendor_plant_id: string;
  name: string;
  capacity_kwp: number | null;
  address: string | null;
  status: PlantStatus;
  updated_at: string;
  latest_kpi: KpiPoint | null;
}

export async function fetchPlants(signal?: AbortSignal): Promise<Plant[]> {
  const response = await fetch(`${API_BASE_URL}/api/v1/plants`, {
    signal,
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error(`API error ${response.status}`);
  }
  return response.json();
}
