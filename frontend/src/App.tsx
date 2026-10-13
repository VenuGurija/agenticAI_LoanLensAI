import { Route, Routes } from "react-router-dom";
import { BookScreen } from "./screens/BookScreen";
import { LoanDetailScreen } from "./screens/LoanDetailScreen";
import { PreviewScreen } from "./screens/PreviewScreen";
import { UploadScreen } from "./screens/UploadScreen";

export default function App() {
  return (
    <div className="app-shell">
      <Routes>
        <Route path="/" element={<BookScreen />} />
        <Route path="/add" element={<UploadScreen />} />
        <Route path="/a/:analysisId" element={<PreviewScreen />} />
        <Route path="/loans/:loanInternalId" element={<LoanDetailScreen />} />
      </Routes>
    </div>
  );
}
