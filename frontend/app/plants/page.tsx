"use client";

// Plants dashboard — reads exclusively from our backend API, which serves
// data ingested by the central scheduler. Never calls vendor APIs.

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchPlants, type Plant } from "@/lib/api";

const REFRESH_INTERVAL_MS = 30_000;

function formatNumber(value: number | null, digits = 1): string {
  if (value === null || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export default function PlantsPage() {
  const [plants, setPlants] = useState<Plant[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const data = await fetchPlants(controller.signal);
      // An aborted request must never update state, even when the fetch
      // itself already resolved before the abort landed.
      if (controller.signal.aborted) return;
      setPlants(data);
      setError(null);
      setUpdatedAt(new Date());
    } catch (err) {
      if (!controller.signal.aborted) {
        setError(err instanceof Error ? err.message : "failed to load plants");
      }
    }
  }, []);

  useEffect(() => {
    // Initial fetch is deferred to a microtask: state updates happen in the
    // async fetch callback, never synchronously inside the effect body.
    // The cancelled flag keeps the queued task from starting a request
    // after the effect has already been cleaned up.
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) void load();
    });
    const timer = setInterval(() => void load(), REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
      abortRef.current?.abort();
    };
  }, [load]);

  return (
    <main>
      <h1>Plants</h1>
      <p className="subtitle">
        Live fleet overview · refreshes every {REFRESH_INTERVAL_MS / 1000}s
      </p>

      {error && (
        <div className="state-note error">
          Could not reach the AQ O&amp;M API: {error}
        </div>
      )}

      {!error && plants === null && (
        <div className="state-note">Loading plants…</div>
      )}

      {!error && plants !== null && plants.length === 0 && (
        <div className="state-note">
          No plants ingested yet. Start the backend with SCHEDULER_ENABLED=true
          to ingest data from the FusionSolar adapter (mock mode by default).
        </div>
      )}

      {!error && plants !== null && plants.length > 0 && (
        <>
          <table className="plants-table">
            <thead>
              <tr>
                <th>Plant</th>
                <th>Status</th>
                <th className="num">Capacity (kWp)</th>
                <th className="num">Power (kW)</th>
                <th className="num">Energy today (kWh)</th>
                <th className="num">PR</th>
              </tr>
            </thead>
            <tbody>
              {plants.map((plant) => (
                <tr key={plant.id}>
                  <td>
                    <strong>{plant.name}</strong>
                    <div className="meta-line" style={{ marginTop: 2 }}>
                      {plant.address ?? plant.vendor_plant_id}
                    </div>
                  </td>
                  <td>
                    <span className={`badge ${plant.status}`}>
                      {plant.status}
                    </span>
                  </td>
                  <td className="num">{formatNumber(plant.capacity_kwp, 0)}</td>
                  <td className="num">
                    {formatNumber(plant.latest_kpi?.active_power_kw ?? null)}
                  </td>
                  <td className="num">
                    {formatNumber(plant.latest_kpi?.daily_energy_kwh ?? null)}
                  </td>
                  <td className="num">
                    {formatPercent(
                      plant.latest_kpi?.performance_ratio ?? null,
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="meta-line">
            {updatedAt
              ? `Last refreshed ${updatedAt.toLocaleTimeString()}`
              : ""}
          </p>
        </>
      )}
    </main>
  );
}
