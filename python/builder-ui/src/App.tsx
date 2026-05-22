import { BrowserRouter, Routes, Route } from 'react-router-dom';

export function App() {
  return (
    <BrowserRouter basename={import.meta.env.BASE_URL}>
      <Routes>
        <Route path="/" element={<div>Hello from /_apx/builder — Phase 0 stub</div>} />
      </Routes>
    </BrowserRouter>
  );
}
