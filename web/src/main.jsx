import React from 'react';
import { createRoot } from 'react-dom/client';
import 'leaflet/dist/leaflet.css';
import 'leaflet-draw/dist/leaflet.draw.css';
import App from './App.jsx';
import './styles.css';
import drawSprite from 'leaflet-draw/dist/images/spritesheet.png';
import drawSprite2x from 'leaflet-draw/dist/images/spritesheet-2x.png';

document.documentElement.style.setProperty('--leaflet-draw-sprite', `url(${drawSprite})`);
document.documentElement.style.setProperty('--leaflet-draw-sprite-2x', `url(${drawSprite2x})`);

const root = createRoot(document.getElementById('root'));
root.render(<App />);
