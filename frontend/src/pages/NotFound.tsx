import { Link } from "react-router-dom";
import { Card, Empty } from "../components/ui";

export function NotFoundPage() {
  return (
    <Card>
      <Empty title="Page not found">
        <p>That address does not exist. <Link to="/">Go to the overview</Link>.</p>
      </Empty>
    </Card>
  );
}
