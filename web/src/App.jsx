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

const TEXT = {
  de: {
    eyebrow: 'Zonal Climate Analyzer',
    title: 'Klimatrends für beliebige Flächen in Deutschland – schnell, einfach und kostenlos.',
    introLead:
      'Definieren Sie eine Fläche direkt auf der Karte oder laden Sie eine Vektordatei hoch. Die Webanwendung erstellt automatisch eine Karte sowie Diagramme mit jährlichen Zeitreihen zu:',
    introList: ['Lufttemperatur', 'Niederschlag', 'Sonnenscheindauer', 'Vegetationsperiode'],
    introSource: 'Die Auswertungen basieren auf offiziellen Klimadaten des Deutschen Wetterdienstes (DWD).',
    introFree: 'Die Nutzung ist kostenlos, ohne Registrierung und ohne Account möglich.',
    donateLead: 'Wenn Sie das Projekt unterstützen möchten, können Sie freiwillig spenden:',
    donateHelp: 'Ihre Unterstützung hilft dabei, diese Anwendung dauerhaft frei zugänglich zu halten.',
    tabDraw: 'Auf der Karte zeichnen',
    tabUpload: 'Datei hochladen',
    hintDraw: 'Zeichne ein Polygon innerhalb der Grenze von Deutschland und klicke auf den Button rechts.',
    actionDraw: 'Gezeichnete Fläche analysieren',
    uploadTitle: 'Shapefile, GeoPackage oder GeoJSON hier ablegen',
    uploadSubtitle: 'oder klicken zum Auswählen',
    uploadButtonIdle: 'Fläche analysieren',
    uploadButtonBusy: 'Upload läuft…',
    acceptedHint: 'Akzeptierte Dateiformate: .zip (Shapefile mit .prj), .gpkg, .geojson',
    tooLarge: `Datei ist zu groß. Maximal ${MAX_UPLOAD_MB} MB.`,
    missingFile: 'Bitte zuerst eine Datei auswählen.',
    invalidType: 'Bitte eine .zip-, .shp-, .gpkg- oder .geojson-Datei hochladen.',
    statusUploading: 'Upload läuft…',
    statusProcessing: 'Rasterstatistiken und Plots werden erstellt…',
    statusDone: 'Diagramme sind fertig.',
    statusError: 'Etwas ist schiefgelaufen.',
    statusIdle: 'Bereit.',
    loadingActive: 'Daten werden verarbeitet. Das kann ein paar Minuten dauern…',
    loadingIdle: 'Bereit',
    analyzeFailed: 'Analyse fehlgeschlagen.',
    analyzeTimeout: 'Analyse dauert zu lange. Bitte später erneut versuchen.',
    unexpectedError: 'Unerwarteter Fehler.',
    resultsTitle: 'Ergebnisse',
    resultsSubtitle: 'Diagramme herunterladen oder die interaktive Karte öffnen.',
    downloadAll: 'Alle Ausgaben herunterladen',
    mapTile: 'Interaktive Karte öffnen',
    mapTileShort: 'Interaktive Karte',
    newTab: 'Neuer Tab',
    footerSupport:
      'Unterstütze dieses Projekt. Deine Spende hilft mir, die Website als Solo-Developer weiterhin kostenlos zu betreiben. Für die Berechnungen werden Daten des Deutscher Wetterdienst genutzt.',
    impressum: 'Impressum'
  },
  en: {
    eyebrow: 'Climate Trends Germany',
    title: 'Climate trends for any area in Germany — fast, transparent, publication-ready.',
    introLead:
      'Define an area directly on the map or upload a vector file. The web application automatically creates a map and charts with annual time series for:',
    introList: ['Air temperature', 'Precipitation', 'Sunshine duration', 'Growing season'],
    introSource:
      'The analyses are based on official climate data from the German Weather Service (DWD).',
    introFree: 'Use is free of charge, with no registration and no account required.',
    donateLead: 'If you want to donate:',
    donateHelp: 'This helps me keep the website free for everyone.',
    tabDraw: 'Draw on the map',
    tabUpload: 'Upload a file',
    hintDraw: 'Draw a polygon inside Germany’s boundary and click the button on the right.',
    actionDraw: 'Analyze drawn area',
    uploadTitle: 'Drop a Shapefile, GeoPackage or GeoJSON here',
    uploadSubtitle: 'or click to select',
    uploadButtonIdle: 'Analyze area',
    uploadButtonBusy: 'Uploading…',
    acceptedHint: 'Accepted file types: .zip (Shapefile with .prj), .gpkg, .geojson',
    tooLarge: `File is too large. Max ${MAX_UPLOAD_MB} MB.`,
    missingFile: 'Please select a file first.',
    invalidType: 'Please upload a .zip, .shp, .gpkg, or .geojson file.',
    statusUploading: 'Uploading…',
    statusProcessing: 'Generating raster stats and plots…',
    statusDone: 'Charts are ready.',
    statusError: 'Something went wrong.',
    statusIdle: 'Ready.',
    loadingActive: 'Processing data. This can take a few minutes…',
    loadingIdle: 'Ready',
    analyzeFailed: 'Analysis failed.',
    analyzeTimeout: 'Analysis is taking too long. Please try again later.',
    unexpectedError: 'Unexpected error.',
    resultsTitle: 'Results',
    resultsSubtitle: 'Download charts or open the interactive map.',
    downloadAll: 'Download all outputs',
    mapTile: 'Open interactive map',
    mapTileShort: 'Interactive map',
    newTab: 'New tab',
    footerSupport:
      'Support this project. Your donation helps me keep the site free as a solo developer. Calculations use data from the German Weather Service.',
    impressum: 'Imprint'
  }
};

const resolveInitialLang = () => {
  if (typeof navigator === 'undefined') return 'de';
  const raw = navigator.language || navigator.userLanguage || '';
  return raw.toLowerCase().startsWith('de') ? 'de' : 'en';
};

function LoadingBar({ active, labelActive, labelIdle }) {
  return (
      <div className={`loading ${active ? 'active' : ''}`}>
        <div className="loading-track">
          <div className="loading-bar" />
        </div>
        <span className="loading-label">
        {active ? labelActive : labelIdle}
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
  const [lang, setLang] = useState(resolveInitialLang);
  const t = TEXT[lang] || TEXT.de;
  const [mode, setMode] = useState('draw');
  const [file, setFile] = useState(null);
  const [drawnGeojson, setDrawnGeojson] = useState(null);
  const [coverageGeojson, setCoverageGeojson] = useState(fallbackCoverage || null);
  const [status, setStatus] = useState('idle');
  const [message, setMessage] = useState('');
  const [results, setResults] = useState([]);
  const [zipUrl, setZipUrl] = useState('');

  const hint = useMemo(() => {
    if (!file) return t.acceptedHint;
    return `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  }, [file, t.acceptedHint]);

  const humanStatus = (state) => {
    switch (state) {
      case 'uploading':
        return t.statusUploading;
      case 'processing':
        return t.statusProcessing;
      case 'done':
        return t.statusDone;
      case 'error':
        return t.statusError;
      default:
        return t.statusIdle;
    }
  };

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
      setMessage(t.tooLarge);
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
      setMessage(t.tooLarge);
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
      setMessage(t.missingFile);
      setStatus('error');
      return;
    }

    const ext = `.${file.name.split('.').pop()}`.toLowerCase();
    if (!ACCEPTED.includes(ext)) {
      setMessage(t.invalidType);
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
        throw new Error(payload?.detail || response.statusText || t.analyzeFailed);
      }

      const outputs = (payload.outputs || []).map((item) => ({
        ...item,
        url: toApiUrl(item.url)
      }));
      setResults(outputs);
      setZipUrl(toApiUrl(payload.zipUrl));
      setStatus('done');
      setMessage(payload.message || t.statusDone);
    } catch (error) {
      setStatus('error');
      if (error.name === 'AbortError') {
        setMessage(t.analyzeTimeout);
      } else {
        setMessage(error.message || t.unexpectedError);
      }
    } finally {
      clearTimeout(processingTimer);
    }
  };

  const submitDraw = async () => {
    if (!drawnGeojson) {
      setMessage(t.hintDraw);
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
        throw new Error(payload?.detail || response.statusText || t.analyzeFailed);
      }

      const outputs = (payload.outputs || []).map((item) => ({
        ...item,
        url: toApiUrl(item.url)
      }));
      setResults(outputs);
      setZipUrl(toApiUrl(payload.zipUrl));
      setStatus('done');
      setMessage(payload.message || t.statusDone);
    } catch (error) {
      setStatus('error');
      if (error.name === 'AbortError') {
        setMessage(t.analyzeTimeout);
      } else {
        setMessage(error.message || t.unexpectedError);
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
      <div className="lang-switch" role="group" aria-label="Language switch">
        <button
          type="button"
          className={`lang-button ${lang === 'de' ? 'active' : ''}`}
          onClick={() => setLang('de')}
          aria-label="Deutsch"
          title="Deutsch"
        >
          🇩🇪
        </button>
        <button
          type="button"
          className={`lang-button ${lang === 'en' ? 'active' : ''}`}
          onClick={() => setLang('en')}
          aria-label="English"
          title="English"
        >
          🇬🇧
        </button>
      </div>
      <header className="hero">
        <div className="eyebrow">{t.eyebrow}</div>
        <h1>{t.title}</h1>
        <div className="intro">
          <p>{t.introLead}</p>
          <ul>
            {t.introList.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
          <p>{t.introSource}</p>
          <p>{t.introFree}</p>
          <p>
            {t.donateLead}{' '}
            <a href="https://buymeacoffee.com/levingiersch" target="_blank" rel="noopener noreferrer">
              buymeacoffee.com/levingiersch
            </a>
          </p>
          <p>{t.donateHelp}</p>
        </div>
      </header>

      <section className="card">
        <div className="tabs">
          <button className={`tab ${mode === 'draw' ? 'active' : ''}`} onClick={() => setMode('draw')}>
            {t.tabDraw}
          </button>
          <button className={`tab ${mode === 'upload' ? 'active' : ''}`} onClick={() => setMode('upload')}>
            {t.tabUpload}
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
                {t.hintDraw}
              </div>
              <button className="primary" onClick={submitDraw} disabled={status === 'uploading' || status === 'processing'}>
                {t.actionDraw}
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
                <span className="title">{t.uploadTitle}</span>
                <span className="subtitle">{t.uploadSubtitle}</span>
              </label>
            </div>
            <div className="meta">
              <div className="hint">{hint}</div>
              <button className="primary" type="submit" disabled={!file || status === 'uploading'}>
                {status === 'uploading' ? t.uploadButtonBusy : t.uploadButtonIdle}
              </button>
            </div>
          </form>
        )}

        <div className={`status ${status}`}>
          <span>{humanStatus(status)}</span>
          {message ? <span className="message">{message}</span> : null}
        </div>
        <LoadingBar
          active={status === 'uploading' || status === 'processing'}
          labelActive={t.loadingActive}
          labelIdle={t.loadingIdle}
        />
      </section>

      {results.length > 0 ? (
        <section className="results">
            <div className="results-header">
              <div>
                <h2>{t.resultsTitle}</h2>
              <p>{t.resultsSubtitle}</p>
              </div>
            <button className="ghost" type="button" onClick={downloadAll} disabled={!zipUrl}>
              {t.downloadAll}
            </button>
          </div>

          <div className="grid">
            {maps.length > 0 ? (
              <a className="plot map-tile" href={maps[0].url} target="_blank" rel="noreferrer">
                <div className="map-tile-visual" aria-hidden="true">
                  <svg viewBox="0 0 64 64" className="map-icon" role="presentation">
                    <path
                      d="M12 14c0-1.1.9-2 2-2h8c.3 0 .6.1.9.2l10.2 4.1c.6.2 1.2.2 1.8 0l10.2-4.1c.3-.1.6-.2.9-.2h8c1.1 0 2 .9 2 2v36c0 1.1-.9 2-2 2h-8c-.3 0-.6-.1-.9-.2l-10.2-4.1c-.6-.2-1.2-.2-1.8 0l-10.2 4.1c-.3.1-.6.2-.9.2h-8c-1.1 0-2-.9-2-2V14z"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinejoin="round"
                    />
                    <path d="M22 12v40" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
                    <path d="M42 12v40" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" />
                    <path
                      d="M16 26c5-4 11-4 16 0s11 4 16 0"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2.5"
                      strokeLinecap="round"
                    />
                  </svg>
                  <span>{t.mapTileShort}</span>
                </div>
                <div className="plot-meta">
                  <span>{maps[0].label || t.mapTile}</span>
                  <span className="tag">{t.newTab}</span>
                </div>
              </a>
            ) : null}
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
          {t.footerSupport}{' '}
          <a href="https://opendata.dwd.de/" target="_blank" rel="noreferrer">
            opendata.dwd.de
          </a>
        </span>
        <div className="impressum">
          <span>{t.impressum}</span>
          <span>Levin Giersch</span>
          <a href="mailto:levin.giersch@tutamail.com">levin.giersch@tutamail.com</a>
          <a href="https://github.com/LevinGiersch" target="_blank" rel="noreferrer">
            github.com/LevinGiersch
          </a>
        </div>
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
