import { Suspense } from "react";
import { InvestigationDetail } from "@/components/InvestigationDetail";

// cacheComponents is on: the dynamic id is only known at request time, so the client
// component that reads it with useParams must sit inside a Suspense boundary.
export default function InvestigationPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <InvestigationDetail />
    </Suspense>
  );
}
