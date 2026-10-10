import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import { LoadingState } from "./components/ui";

const DashboardPage = lazy(() => import("./features/dashboard/DashboardPage"));
const RunsPage = lazy(() => import("./features/runs/RunsPage"));
const RunDetailPage = lazy(() => import("./features/runs/RunDetailPage"));
const ReportsPage = lazy(() => import("./features/reports/ReportsPage"));
const ReportDetailPage = lazy(() => import("./features/reports/ReportDetailPage"));
const CompaniesPage = lazy(() => import("./features/companies/CompaniesPage"));
const SettingsPage = lazy(() => import("./features/settings/SettingsPage"));

export default function App() {
  return (
    <Suspense fallback={<LoadingState message="Loading page…" />}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunDetailPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route path="reports/:reportId" element={<ReportDetailPage />} />
          <Route path="companies" element={<CompaniesPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
