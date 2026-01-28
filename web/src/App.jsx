import React, { useCallback, useMemo, useState } from 'react';
import { GeoJSON, MapContainer, TileLayer, useMap } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet-draw';
import fallbackCoverage from './assets/germany_coverage.json';

const ACCEPTED = ['.zip', '.shp', '.gpkg', '.geojson'];
const MAX_UPLOAD_MB = 200;
const API_BASE = (import.meta.env.VITE_API_BASE || '').replace(/\/$/, '');
const apiUrl = (path) => `${API_BASE}${path}`;
const COVERAGE_TIMEOUT_MS = 2500;
const ANALYZE_TIMEOUT_MS = 20 * 60 * 1000;

// Fix default marker icons for Vite

delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: new URL('leaflet/dist/images/marker-icon-2x.png', import.meta.url).href,
  iconUrl: new URL('leaflet/dist/images/marker-icon.png', import.meta.url).href,
  shadowUrl: new URL('leaflet/dist/images/marker-shadow.png', import.meta.url).href
});

function humanStatus(state) {
  switch (state) {
    case 'uploading':
      return 'Upload läuft…';
    case 'processing':
      return 'Rasterstatistiken und Plots werden erstellt…';
    case 'done':
      return 'Diagramme sind fertig.';
    case 'error':
      return 'Etwas ist schiefgelaufen.';
    default:
      return 'Bereit.';
  }
}

function LoadingBar({ active }) {
  return (
      <div className={`loading ${active ? 'active' : ''}`}>
        <div className="loading-track">
          <div className="loading-bar" />
        </div>
        <span className="loading-label">
        {active ? 'Daten werden verarbeitet. Das kann ein paar Minuten dauern…' : 'Bereit'}
        </span>
      </div>
  );
}

function DrawControl({ onCreated, onDeleted }) {
  const map = useMap();

  React.useEffect(() => {
    const drawnItems = new L.FeatureGroup();
    map.addLayer(drawnItems);

    const drawControl = new L.Control.Draw({
      edit: false,
      draw: {
        polygon: true,
        rectangle: false,
        circle: false,
        marker: false,
        polyline: false,
        circlemarker: false
      }
    });

    map.addControl(drawControl);

    const handleCreate = (event) => {
      drawnItems.clearLayers();
      drawnItems.addLayer(event.layer);
      onCreated(event.layer.toGeoJSON());
    };

    const handleDeleted = () => {
      drawnItems.clearLayers();
      onDeleted();
    };

    map.on(L.Draw.Event.CREATED, handleCreate);
    map.on(L.Draw.Event.DELETED, handleDeleted);

    return () => {
      map.off(L.Draw.Event.CREATED, handleCreate);
      map.off(L.Draw.Event.DELETED, handleDeleted);
      map.removeControl(drawControl);
      map.removeLayer(drawnItems);
    };
  }, [map, onCreated, onDeleted]);

  return null;
}

function MapSizer() {
  const map = useMap();

  React.useEffect(() => {
    const handleResize = () => map.invalidateSize();
    const timer = setTimeout(() => map.invalidateSize(), 200);
    const timer2 = setTimeout(() => map.invalidateSize(), 800);
    const container = map.getContainer();
    let observer;
    if (container && 'ResizeObserver' in window) {
      observer = new ResizeObserver(() => map.invalidateSize());
      observer.observe(container);
    }
    window.addEventListener('resize', handleResize);
    return () => {
      clearTimeout(timer);
      clearTimeout(timer2);
      window.removeEventListener('resize', handleResize);
      if (observer) {
        observer.disconnect();
      }
    };
  }, [map]);

  return null;
}

function getBoundsFromGeojson(geojson) {
  if (!geojson) return null;
  if (Array.isArray(geojson.bbox) && geojson.bbox.length >= 4) {
    const [minLng, minLat, maxLng, maxLat] = geojson.bbox;
    return [[minLat, minLng], [maxLat, maxLng]];
  }
  const collectCoords = (node, acc) => {
    if (!node) return;
    if (node.type === 'Feature') {
      collectCoords(node.geometry, acc);
      return;
    }
    if (node.type === 'FeatureCollection') {
      node.features.forEach((feature) => collectCoords(feature, acc));
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((item) => collectCoords(item, acc));
      return;
    }
    if (node.coordinates) {
      collectCoords(node.coordinates, acc);
      return;
    }
    if (typeof node[0] === 'number' && typeof node[1] === 'number') {
      acc.push(node);
      return;
    }
  };
  const coords = [];
  collectCoords(geojson, coords);
  if (!coords.length) return null;
  const lats = coords.map((c) => c[1]);
  const lngs = coords.map((c) => c[0]);
  const southWest = [Math.min(...lats), Math.min(...lngs)];
  const northEast = [Math.max(...lats), Math.max(...lngs)];
  return [southWest, northEast];
}

function buildGermanyMask(geojson) {
  const outer = [
    [-90, -180],
    [-90, 180],
    [90, 180],
    [90, -180],
    [-90, -180]
  ];

  const holes = [];
  const addRing = (ring) => {
    holes.push(ring);
  };

  if (geojson.type === 'Polygon') {
    geojson.coordinates.forEach(addRing);
  } else if (geojson.type === 'MultiPolygon') {
    geojson.coordinates.forEach((poly) => poly.forEach(addRing));
  }

  return {
    type: 'Polygon',
    coordinates: [outer, ...holes]
  };
}

function GermanyOverlay({ geojson }) {
  const map = useMap();

  React.useEffect(() => {
    if (!geojson) return;
    const bounds = getBoundsFromGeojson(geojson);
    if (bounds) {
      map.fitBounds(bounds, { padding: [20, 20] });
    }
  }, [geojson, map]);

  if (!geojson) return null;

  const mask = buildGermanyMask(geojson);

  return (
    <>
      <GeoJSON
        data={mask}
        style={{
          fillColor: '#0c0f12',
          fillOpacity: 0.55,
          stroke: false
        }}
        interactive={false}
      />
      <GeoJSON
        data={geojson}
        style={{
          color: '#1d2328',
          weight: 2,
          fillOpacity: 0
        }}
        interactive={false}
      />
    </>
  );
}

function FitToBounds({ bounds }) {
  const map = useMap();

  React.useEffect(() => {
    if (!bounds) return;
    map.fitBounds(bounds, { padding: [20, 20] });
  }, [bounds, map]);

  return null;
}

async function readJson(response) {
  try {
    return await response.json();
  } catch (error) {
    return null;
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 0) {
  if (!timeoutMs) {
    return fetch(url, options);
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

function toApiUrl(url) {
  if (!url) return '';
  if (/^https?:\/\//i.test(url)) return url;
  if (url.startsWith('/')) {
    return apiUrl(url);
  }
  return apiUrl(`/${url}`);
}

export default function App() {
  const [mode, setMode] = useState('draw');
  const [file, setFile] = useState(null);
  const [drawnGeojson, setDrawnGeojson] = useState(null);
  const [coverageGeojson, setCoverageGeojson] = useState(fallbackCoverage || null);
  const [status, setStatus] = useState('idle');
  const [message, setMessage] = useState('');
  const [results, setResults] = useState([]);
  const [zipUrl, setZipUrl] = useState('');

  const hint = useMemo(() => {
    if (!file) return 'Akzeptierte Dateiformate: .zip (Shapefile mit .prj), .gpkg, .geojson';
    return `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  }, [file]);

  const resetStatus = () => {
    setResults([]);
    setMessage('');
    setStatus('idle');
    setZipUrl('');
  };

  React.useEffect(() => {
    const loadCoverage = async () => {
      if (fallbackCoverage) {
        setCoverageGeojson(fallbackCoverage);
      }
      try {
        const response = await fetchWithTimeout(
          apiUrl('/api/coverage'),
          {},
          COVERAGE_TIMEOUT_MS
        );
        const payload = await readJson(response);
        if (response.ok && payload?.geojson) {
          setCoverageGeojson(payload.geojson);
          return;
        }
      } catch (error) {
        // Fall back to bundled coverage if the API is unavailable.
      }
    };
    loadCoverage();
  }, []);

  const onFileChange = (event) => {
    const next = event.target.files?.[0] || null;
    if (next && next.size / 1024 / 1024 > MAX_UPLOAD_MB) {
      setFile(null);
      setMessage(`Datei ist zu groß. Maximal ${MAX_UPLOAD_MB} MB.`);
      setStatus('error');
      return;
    }
    setFile(next);
    resetStatus();
  };

  const onDrop = (event) => {
    event.preventDefault();
    const next = event.dataTransfer.files?.[0] || null;
    if (next && next.size / 1024 / 1024 > MAX_UPLOAD_MB) {
      setFile(null);
      setMessage(`Datei ist zu groß. Maximal ${MAX_UPLOAD_MB} MB.`);
      setStatus('error');
      return;
    }
    setFile(next);
    resetStatus();
  };

  const onDragOver = (event) => {
    event.preventDefault();
  };

  const submitUpload = async (event) => {
    event.preventDefault();
    if (!file) {
      setMessage('Bitte zuerst eine Datei auswählen.');
      setStatus('error');
      return;
    }

    const ext = `.${file.name.split('.').pop()}`.toLowerCase();
    if (!ACCEPTED.includes(ext)) {
      setMessage('Bitte eine .zip-, .shp-, .gpkg- oder .geojson-Datei hochladen.');
      setStatus('error');
      return;
    }

    setStatus('uploading');
    setMessage('');
    const processingTimer = setTimeout(() => {
      setStatus('processing');
    }, 1200);

    const body = new FormData();
    body.append('file', file);

    try {
      const response = await fetchWithTimeout(
        apiUrl('/api/analyze'),
        { method: 'POST', body },
        ANALYZE_TIMEOUT_MS
      );

      const payload = await readJson(response);
      if (!response.ok) {
        throw new Error(payload?.detail || response.statusText || 'Analyse fehlgeschlagen.');
      }

      const outputs = (payload.outputs || []).map((item) => ({
        ...item,
        url: toApiUrl(item.url)
      }));
      setResults(outputs);
      setZipUrl(toApiUrl(payload.zipUrl));
      setStatus('done');
      setMessage(payload.message || 'Erfolg.');
    } catch (error) {
      setStatus('error');
      if (error.name === 'AbortError') {
        setMessage('Analyse dauert zu lange. Bitte später erneut versuchen.');
      } else {
        setMessage(error.message || 'Unerwarteter Fehler.');
      }
    } finally {
      clearTimeout(processingTimer);
    }
  };

  const submitDraw = async () => {
    if (!drawnGeojson) {
      setMessage('Bitte zuerst ein Polygon auf der Karte zeichnen.');
      setStatus('error');
      return;
    }

    setStatus('uploading');
    setMessage('');
    const processingTimer = setTimeout(() => {
      setStatus('processing');
    }, 1200);

    try {
      const response = await fetchWithTimeout(
        apiUrl('/api/analyze-geojson'),
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ geojson: drawnGeojson })
        },
        ANALYZE_TIMEOUT_MS
      );

      const payload = await readJson(response);
      if (!response.ok) {
        throw new Error(payload?.detail || response.statusText || 'Analyse fehlgeschlagen.');
      }

      const outputs = (payload.outputs || []).map((item) => ({
        ...item,
        url: toApiUrl(item.url)
      }));
      setResults(outputs);
      setZipUrl(toApiUrl(payload.zipUrl));
      setStatus('done');
      setMessage(payload.message || 'Erfolg.');
    } catch (error) {
      setStatus('error');
      if (error.name === 'AbortError') {
        setMessage('Analyse dauert zu lange. Bitte später erneut versuchen.');
      } else {
        setMessage(error.message || 'Unerwarteter Fehler.');
      }
    } finally {
      clearTimeout(processingTimer);
    }
  };

  const plots = results.filter((item) => item.type === 'plot');
  const maps = results.filter((item) => item.type === 'map');

  const handleCreated = useCallback((geojson) => {
    setDrawnGeojson(geojson);
    resetStatus();
  }, []);

  const handleDeleted = useCallback(() => {
    setDrawnGeojson(null);
    resetStatus();
  }, []);

  const downloadAll = useCallback(() => {
    if (!zipUrl) return;
    const link = document.createElement('a');
    link.href = zipUrl;
    link.download = '';
    link.rel = 'noreferrer';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }, [zipUrl]);

  const coverageBounds = useMemo(() => getBoundsFromGeojson(coverageGeojson), [coverageGeojson]);

  return (
    <div className="page">
      <div className="glow" />
      <header className="hero">
        <div className="eyebrow">Klimatrends Deutschland</div>
        <h1>Klimatrends für jede Fläche in Deutschland – schnell, nachvollziehbar, publikationsreif.</h1>
        <p>
          Zeichne ein Polygon oder lade eine Vektordatei hoch. Wir erzeugen eine Karte sowie Diagramme mit jährlichen Werten für Temperatur, Niederschlag, Sonnenschein und Vegetationsperiode für die Fläche seit 1951. Dazu werden offizielle Daten des Deutschen Wetterdienst genutzt.
          Das alle gratis und ohne Account!
        </p>
      </header>

      <section className="card">
        <div className="tabs">
          <button className={`tab ${mode === 'draw' ? 'active' : ''}`} onClick={() => setMode('draw')}>
            Auf der Karte zeichnen
          </button>
          <button className={`tab ${mode === 'upload' ? 'active' : ''}`} onClick={() => setMode('upload')}>
            Datei hochladen
          </button>
        </div>

        {mode === 'draw' ? (
          <div className="map-wrap">
            <div className="map-card">
              <MapContainer
                center={[51.2, 10.4]}
                zoom={6}
                bounds={coverageBounds || undefined}
                boundsOptions={{ padding: [20, 20] }}
                scrollWheelZoom
              >
                <TileLayer
                  attribution='&copy; OpenStreetMap contributors'
                  url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                />
                <FitToBounds bounds={coverageBounds} />
                <GermanyOverlay geojson={coverageGeojson} />
                <MapSizer />
                <DrawControl onCreated={handleCreated} onDeleted={handleDeleted} />
              </MapContainer>
            </div>
            <div className="map-actions">
              <div className="hint">
                Zeichne ein Polygon innerhalb der Grenze von Deutschland und klicke auf den Button rechts.
              </div>
              <button className="primary" onClick={submitDraw} disabled={status === 'uploading' || status === 'processing'}>
                Gezeichnete Fläche analysieren
              </button>
            </div>
          </div>
        ) : (
          <form className="upload" onSubmit={submitUpload}>
            <div className="dropzone" onDrop={onDrop} onDragOver={onDragOver}>
              <input
                id="file"
                type="file"
                onChange={onFileChange}
                accept={ACCEPTED.join(',')}
              />
              <label htmlFor="file">
                <span className="title">Shapefile, GeoPackage oder GeoJSON hier ablegen</span>
                <span className="subtitle">oder klicken zum Auswählen</span>
              </label>
            </div>
            <div className="meta">
              <div className="hint">{hint}</div>
              <button className="primary" type="submit" disabled={!file || status === 'uploading'}>
                {status === 'uploading' ? 'Upload läuft…' : 'Fläche analysieren'}
              </button>
            </div>
          </form>
        )}

        <div className={`status ${status}`}>
          <span>{humanStatus(status)}</span>
          {message ? <span className="message">{message}</span> : null}
        </div>
        <LoadingBar active={status === 'uploading' || status === 'processing'} />
      </section>

      {results.length > 0 ? (
        <section className="results">
            <div className="results-header">
              <div>
                <h2>Ergebnisse</h2>
              <p>Diagramme herunterladen oder die interaktive Karte öffnen.</p>
              </div>
            <button className="ghost" type="button" onClick={downloadAll} disabled={!zipUrl}>
              Alle Ausgaben herunterladen
            </button>
          </div>

          {maps.length > 0 ? (
            <div className="map">
              <iframe title="karte" src={maps[0].url} loading="lazy" />
            </div>
          ) : null}

          <div className="grid">
            {plots.map((plot) => (
              <a key={plot.url} className="plot" href={plot.url} target="_blank" rel="noreferrer">
                <img src={plot.url} alt={plot.label || plot.name} loading="lazy" />
                <div className="plot-meta">
                  <span>{plot.label || plot.name}</span>
                </div>
              </a>
            ))}
          </div>
        </section>
      ) : null}

      <footer className="footer">
        <span>
          Unterstütze dieses Projekt. Deine Spende hilft mir, die Website als Solo-Developer
          weiterhin kostenlos zu betreiben. Für die Berechnungen werden Daten des Deutscher
          Wetterdienst genutzt.{" "}
          <a href="https://opendata.dwd.de/" target="_blank" rel="noreferrer">
            opendata.dwd.de
          </a>
        </span>
        <a
          className="bmc-button"
          href="https://www.buymeacoffee.com/levingiersch"
          target="_blank"
          rel="noreferrer"
        >
          <img
            src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png"
            alt="Buy Me A Coffee"
            loading="lazy"
          />
        </a>
      </footer>
    </div>
  );
}
