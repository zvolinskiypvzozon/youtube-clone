import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { createQueryClient } from "./api/queryClient";
import { AuthProvider } from "./auth/AuthContext";
import { RequireAuth } from "./auth/RequireAuth";
import { Layout } from "./components/Layout";
import { HomePage } from "./pages/HomePage";
import { LoginPage } from "./pages/LoginPage";
import { ComingSoonPage, NotFoundPage } from "./pages/PlaceholderPages";
import { RegisterPage } from "./pages/RegisterPage";
import { WatchPage } from "./pages/watch/WatchPage";

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<HomePage />} />
        <Route path="login" element={<LoginPage />} />
        <Route path="register" element={<RegisterPage />} />
        <Route path="watch/:videoId" element={<WatchPage />} />
        <Route path="channel/:handle" element={<ComingSoonPage title="Канал" />} />
        <Route
          path="upload"
          element={
            <RequireAuth>
              <ComingSoonPage title="Загрузка видео" />
            </RequireAuth>
          }
        />
        <Route
          path="my/videos"
          element={
            <RequireAuth>
              <ComingSoonPage title="Мои видео" />
            </RequireAuth>
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

export function App() {
  const [queryClient] = useState(createQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <AppRoutes />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  );
}
