import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import './styles/base.css';
import './styles/theme.css';
import './styles/interface.css';

const container = document.getElementById('root');
if (!container) throw new Error('#root is missing from index.html');

// Nothing in this interface is selectable, draggable or right-clickable — it is
// an appliance surface, not a document.
document.addEventListener('contextmenu', (event) => event.preventDefault());
document.addEventListener('dragover', (event) => event.preventDefault());
document.addEventListener('drop', (event) => event.preventDefault());

createRoot(container).render(
  <React.StrictMode>
    <ErrorBoundary label="N.O.V.A. could not start">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
