import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import AuthGate from './AuthGate.jsx'
import { installGlobalCapture } from './voiceDebug.js'
import './index.css'

// #304: uncaught errors and unhandled rejections join the voice
// diagnostics ring from the first render, so a red error during a voice
// stall is in the one-tap dump even when nothing else caught it.
installGlobalCapture()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </React.StrictMode>,
)
