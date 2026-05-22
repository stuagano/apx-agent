import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AgentEditor } from './pages/AgentEditor';

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<AgentEditor />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
