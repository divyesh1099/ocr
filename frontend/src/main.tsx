import React from 'react';
import ReactDOM from 'react-dom/client';
import { Amplify } from 'aws-amplify';
import { BrowserRouter } from 'react-router-dom';
import '@aws-amplify/ui-react/styles.css';
import './styles.css';
import App from './App';
import { setConfig, type AppConfig } from './api';

async function boot() {
  const root = ReactDOM.createRoot(document.getElementById('root')!);
  try {
    const cfg: AppConfig = await (await fetch('/config.json', { cache: 'no-store' })).json();
    setConfig(cfg);
    Amplify.configure({
      Auth: { Cognito: { userPoolId: cfg.userPoolId, userPoolClientId: cfg.userPoolClientId, loginWith: { email: true } } },
    });
    root.render(
      <React.StrictMode>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </React.StrictMode>,
    );
  } catch {
    root.render(<div className="boot-error">Missing <code>/config.json</code>. Copy <code>public/config.example.json</code> to <code>public/config.json</code> for local development.</div>);
  }
}
boot();
