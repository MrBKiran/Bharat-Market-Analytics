import { Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { BacktestPage } from "./pages/Backtest";
import { ComparePage } from "./pages/Compare";
import { DataPage } from "./pages/Data";
import { NotFoundPage } from "./pages/NotFound";
import { OverviewPage } from "./pages/Overview";
import { PerformancePage } from "./pages/Performance";
import { ReportsPage } from "./pages/Reports";
import { SearchPage } from "./pages/Search";
import { SectorsPage } from "./pages/Sectors";
import { StockPage } from "./pages/Stock";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<OverviewPage />} />
        <Route path="stock" element={<StockPage />} />
        <Route path="compare" element={<ComparePage />} />
        <Route path="sectors" element={<SectorsPage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="backtest" element={<BacktestPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="performance" element={<PerformancePage />} />
        <Route path="data" element={<DataPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
